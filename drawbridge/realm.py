
from twisted.words import ewords

from twisted.internet import defer
from twisted.python import failure
from twisted.words import service, iwords

import json
import chord
from unidecode import unidecode

from discord import Message, Server, Channel, PrivateChannel
from discord import User as DiscordUser

from random import randint as random_integer

from zope.interface import implements
from Queue import Queue
from threading import Thread

class DiscordWordsRealm(service.WordsRealm):
    _encoding = 'utf-8'

    def __init__(self, *a, **kw):
        if 'name' not in kw and len(a) < 1:
            kw['name'] = 'discord.gg'
        super(DiscordWordsRealm, self).__init__(*a, **kw)
        self.users = {}
        self.guilds = {}

    def itergroups(self):
        return defer.succeed(self.guilds.itervalues())

    def userFactory(self, name, credentials=None):
        return User(name, credentials=credentials)

    def groupFactory(self, name, server, channel):
        return Group(name, server, channel)

    def logoutFactory(self, avatar, facet):
        def logout():
            # XXX Deferred support here
            getattr(facet, 'logout', lambda: None)()
            avatar.realm = avatar.mind = None
        return logout

    def requestAvatar(self, avatarId, mind, *interfaces):
        if isinstance(avatarId, str):
            avatarId = avatarId.decode(self._encoding)

        def gotAvatar(avatar):
            if avatar.realm is not None:
                raise ewords.AlreadyLoggedIn()
            for iface in interfaces:
                facet = iface(avatar, None)
                if facet is not None:
                    avatar.loggedIn(self, mind)
                    mind.name = avatar.credentials.meta['username']
                    mind.realm = self
                    mind.avatar = avatar
                    return iface, facet, self.logoutFactory(avatar, facet)
            raise NotImplementedError(self, interfaces)

        return self.getUser(credentials.meta['username'], credentials).addCallback(gotAvatar)

    def addUser(self, user):
        if user.name in self.users:
            return defer.fail(failure.Failure(ewords.DuplicateUser()))
        self.users[user.name] = user
        return defer.succeed(user)


    def addGroup(self, group):
        if group.name in self.guilds:
            return defer.fail(failure.Failure(ewords.DuplicateGroup()))
        self.guilds[group.name] = group
        return defer.succeed(group)


    def lookupUser(self, name):
        assert isinstance(name, unicode)
        name = name.lower()
        try:
            user = self.users[name]
        except KeyError:
            return defer.fail(failure.Failure(ewords.NoSuchUser(name)))
        else:
            return defer.succeed(user)


    def lookupGroup(self, name):
        assert isinstance(name, unicode)
        name = name.lower()
        try:
            group = self.guilds[name]
        except KeyError:
            return defer.fail(failure.Failure(ewords.NoSuchGroup(name)))
        else:
            return defer.succeed(group)

    def getGroup(self, name):
        assert isinstance(name, unicode)
        return self.lookupGroup(name)


    def getUser(self, id, credentials=None):
        assert isinstance(id, unicode)
        assert credentials is not None
        if self.createUserOnRequest:
            def ebUser(err):
                err.trap(ewords.DuplicateUser)
                return self.lookupUser(id)
            return self.createUser(id, credentials).addErrback(ebUser)
        return self.lookupUser(id)


    def createUser(self, id, credentials=None):
        assert isinstance(id, unicode)
        assert credentials is not None
        def cbLookup(user):
            return failure.Failure(ewords.DuplicateUser(id))
        def ebLookup(err):
            err.trap(ewords.NoSuchUser)
            return self.userFactory(id, credentials)

        name = credentials.meta['username'].lower()
        d = self.lookupUser(name)
        d.addCallbacks(cbLookup, ebLookup)
        d.addCallback(self.addUser)
        return d


    def createGroup(self, server, channel):
        name = channel.name.replace(' ', '_')
        assert isinstance(name, unicode)
        def cbLookup(group):
            return failure.Failure(ewords.DuplicateGroup(name))
        def ebLookup(err):
            err.trap(ewords.NoSuchGroup)
            return self.groupFactory(name, server, channel)

        name = name.lower()
        d = self.lookupGroup(name)
        d.addCallbacks(cbLookup, ebLookup)
        d.addCallback(self.addGroup)
        return d

class Group(object):
    implements(iwords.IGroup)

    def __init__(self, name, server, channel):
        self.name = name
        self.server = server
        self.channel = channel
        self.users = {}
        self.meta = {
            "topic": channel.topic,
            "topic_author": "discord",
            }


    def _ebUserCall(self, err, p):
        return failure.Failure(Exception(p, err))


    def _cbUserCall(self, results):
        for (success, result) in results:
            if not success:
                user, err = result.value # XXX
                self.remove(user, err.getErrorMessage())


    def add(self, user):
        assert iwords.IChatClient.providedBy(user), "%r is not a chat client" % (user,)
        if user.name not in self.users:
            additions = []
            self.users[user.name] = user
            for p in self.users.itervalues():
                if p is not user:
                    d = defer.maybeDeferred(p.userJoined, self, user)
                    d.addErrback(self._ebUserCall, p=p)
                    additions.append(d)
            defer.DeferredList(additions).addCallback(self._cbUserCall)
        return defer.succeed(None)


    def remove(self, user, reason=None):
        assert reason is None or isinstance(reason, unicode)
        try:
            del self.users[user.name]
        except KeyError:
            pass
        else:
            removals = []
            for p in self.users.itervalues():
                if p is not user:
                    d = defer.maybeDeferred(p.userLeft, self, user, reason)
                    d.addErrback(self._ebUserCall, p=p)
                    removals.append(d)
            defer.DeferredList(removals).addCallback(self._cbUserCall)
        return defer.succeed(None)


    def size(self):
        return defer.succeed(len(self.users))


    def receive(self, sender, recipient, message):
        assert recipient is self
        receives = []
        for p in self.users.itervalues():
            if p is not sender:
                d = defer.maybeDeferred(p.receive, sender, self, message)
                d.addErrback(self._ebUserCall, p=p)
                receives.append(d)
        defer.DeferredList(receives).addCallback(self._cbUserCall)
        return defer.succeed(None)


    def setMetadata(self, meta):
        self.meta = meta
        sets = []
        for p in self.users.itervalues():
            d = defer.maybeDeferred(p.groupMetaUpdate, self, meta)
            d.addErrback(self._ebUserCall, p=p)
            sets.append(d)
        defer.DeferredList(sets).addCallback(self._cbUserCall)
        return defer.succeed(None)


    def iterusers(self):
        # XXX Deferred?
        return iter(self.users.values())


class DiscordClient(chord.Client):
    mind = None

    def handle_error(self, failure):
        self.mind.svc_message('Uncaught error ' + str(failure.value))
        failure.raiseException()

    def login(self, token, reactor=None):
        self.deferred = self.fetch_gateway(token)

        self.deferred.addErrback(self.handle_error)
        self.deferred.addCallback(self.connect)

        return self.deferred


class User(DiscordClient):
    implements(iwords.IUser)

    meta = None
    realm = None
    _protocol = None

    server_id = None
    gateway = ''
    realName = ''
    _lazy_guilds = 0

    _sent_nonces = []

    def __init__(self, name, credentials=None, reactor=None):
        if reactor is None:
            from twisted.internet import reactor

        self.reactor = reactor
        self.name = name
        self.id = name
        self.groups = []
        self.credentials = credentials
        if self.credentials:
            self.set_meta(credentials.meta)

    def loggedIn(self, realm, mind):
        self.realm = realm
        self.mind = mind
        if self.credentials:
            defer.maybeDeferred(self.login, self.credentials.token)

    def join(self, group):
        def cbJoin(result):
            self.groups.append(group)
            return result
        return group.add(self.mind).addCallback(cbJoin)


    def leave(self, group, reason=None):
        def cbLeave(result):
            self.groups.remove(group)
            return result
        return group.remove(self.mind, reason).addCallback(cbLeave)


    def send(self, recipient, message):
        # Send to IRC first
        recipient.receive(self.mind, recipient, message)

        message = message.get('text', '')
        if message.startswith('\x01ACTION '):
            message = message.replace('ACTION ', '', 1).replace('\x01', '_')

        channel_id = None
        if isinstance(recipient, Group):
            server = self.mind.get_guild(self.server_id)
            for chan in server.channels:
                irc_chan_name = chan.name.replace(' ', '_')
                if irc_chan_name == recipient.name:
                    channel_id = chan.id

        url = 'https://discordapp.com/api/channels/{0}/messages'.format(channel_id)
        nonce = random_integer(-2**63, 2**63 - 1)
        payload = {
            'content': unicode(message),
            'nonce': nonce
        }
        self._sent_nonces.append(unicode(nonce))

        return chord.http_post(url, self.credentials.token, payload)


    def itergroups(self):
        return iter(self.groups)


    def logout(self):
        self.disconnect(u'Leaving...')
        if (self._protocol):
            self._protocol.sendClose()
        for g in self.groups[:]:
            self.leave(g)

    def set_meta(self, meta):
        self.meta = meta
        self.server_id = meta.get('default_server_id', self.server_id)
        self.realName = '{username}#{discriminator}'.format(**meta)
        self.name = meta['username']
        self.id = meta['id']

    # Start discord parsing

    def on_ready(self, data):
        self.mind.svc_message('Connection to discord established.')

        self.gateway = data.get('_trace', ('unknown gateway',))[0]

        self.set_meta(data.get('user'))

        for guild in data.get('guilds', []):
            unavailable = guild.get('unavailable', None)

            if unavailable is None or unavailable is False: # it's available!
                server = Server(**guild)
                server.me = property(lambda s: s.get_member(self.user.id))

                self.mind.add_guild(server)

                if self.server_id is None:
                    self.server_id = server.id

                if self.server_id == server.id:
                    def join_fail(err):
                        print(err)

                    def join_success(_, s, channel):
                        self.mind.names(self.mind.nickname, '#' + channel.name.replace(' ', '_'), [member.name.replace(' ', '_') for member in channel.members])
                        self.mind.topic(self.mind.nickname, '#' + channel.name.replace(' ', '_'), channel.topic)

                    for chan in server.channels:
                        if str(chan.type) != 'text':
                            continue
                        if not chan.permissions_for(chan.server.get_member(self.id)).read_messages:
                            continue
                        d = self.realm.createGroup(chan.server, chan)
                        d.addCallback(self.mind.userJoined, self.mind)
                        d.addCallback(join_success, server, chan)
                        d.addErrback(join_fail)
                pass
            else:
                self._lazy_guilds = self._lazy_guilds + 1

        for pm in data.get('private_channels', []):
            self.mind.add_private_channel(PrivateChannel(id=pm['id'], user=DiscordUser(**pm['recipient'])))

        # def join_fail(err):
        #     print(err)
        # for chan in self.get_all_channels():
        #     if chan.type != 'text':
        #         continue
        #     d = self.realm.createGroup(chan.server, chan)
        #     d.addCallback(self.mind.userJoined, self.mind)
        #     d.addErrback(join_fail)

    def change_nick(self, nick, password):
        payload = {
            'username': nick,
            'password': password,
            'email': self.meta['email'],
            'avatar': self.meta['avatar']
        }

        def handle_success(body):
            self.set_meta(json.loads(body))

        d = chord.http_patch('https://discordapp.com/api/users/@me', self.credentials.token, payload)
        d.addCallback(handle_success)
        return d

    def update_user(self, presence):
        pass

    def on_typing_start(self, data):
        # Literally unused
        pass

    def on_presence_update(self, data):
        # Unused atm
        pass

    def on_user_update(self, data):
        self.mind.svc_message(repr(data))

    def on_message_create(self, data):
        channel = self.mind.get_channel(data.get('channel_id'))
        nonce = data.get('nonce', None)
        if nonce in self._sent_nonces:
            self._sent_nonces.remove(nonce)
            return # We've already got this message in our client
        if (channel and channel.server.id == self.server_id):
            message = Message(channel=channel, **data)
            sender = '{}!{}@discord.gg'.format(unidecode(message.author.name).replace(' ', '_'), message.author.discriminator)
            for line in message.clean_content.split('\n'):
                self.mind.privmsg(sender, '#' + channel.name.replace(' ', '_'), line)
