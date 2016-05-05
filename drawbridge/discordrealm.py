
from twisted.words import ewords

from twisted.internet import defer
from twisted.python import failure
from twisted.words import service, iwords

import chord

from zope.interface import implements
from time import time
from Queue import Queue
from threading import Thread

class DiscordWordsRealm(service.WordsRealm):
    _encoding = 'utf-8'

    def __init__(self, *a, **kw):
        super(DiscordWordsRealm, self).__init__(*a, **kw)
        self.users = {}
        self.guilds = {}

    def itergroups(self):
        return defer.succeed(self.guilds.itervalues())

    def userFactory(self, name, credentials={}):
        return User(name, credentials)

    def groupFactory(self, name, server, channel):
        return Group(name, server, channel)

    def logoutFactory(self, avatar, facet):
        def logout():
            # XXX Deferred support here
            getattr(facet, 'logout', lambda: None)()
            avatar.realm = avatar.mind = None
        return logout

    def requestAvatar(self, avatarId, credentials, mind, *interfaces):
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

        return self.getUser(avatarId, credentials).addCallback(gotAvatar)

    def addUser(self, user):
        print('addUser ' + repr(user))
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
        print('lookupUser ' + repr(name))
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


    def getUser(self, name, credentials={}):
        assert isinstance(name, unicode)
        if self.createUserOnRequest:
            def ebUser(err):
                err.trap(ewords.DuplicateUser)
                return self.lookupUser(name)
            return self.createUser(name, credentials).addErrback(ebUser)
        return self.lookupUser(name)


    def createUser(self, name, credentials={}):
        assert isinstance(name, unicode)
        def cbLookup(user):
            return failure.Failure(ewords.DuplicateUser(name))
        def ebLookup(err):
            err.trap(ewords.NoSuchUser)
            return self.userFactory(name, credentials)

        name = name.lower()
        d = self.lookupUser(name)
        d.addCallbacks(cbLookup, ebLookup)
        d.addCallback(self.addUser)
        return d


    def createGroup(self, server, channel):
        name = server.name.replace(' ', '_') + "|" + channel.name.replace(' ', '_')
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

    realm = None

    def __init__(self, name, credentials=None, reactor=None):
        if reactor is None:
            from twisted.internet import reactor
        self.reactor = reactor
        self.name = name
        self.groups = []
        self.lastMessage = time()
        self.credentials = credentials

    def loggedIn(self, realm, mind):
        self.realm = realm
        self.mind = mind
        self.signOn = time()
        if self.credentials:
            defer.maybeDeferred(self.login, self.credentials.token)

    def on_ready(self, data):
        self.mind.svc_message('Connection to discord established.')
        def join_fail(err):
            print(err)
        for chan in self.get_all_channels():
            if chan.type != 'text':
                continue
            d = self.realm.createGroup(chan.server, chan)
            d.addCallback(self.mind.userJoined, self.mind)
            d.addErrback(join_fail)

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
        # Translate to Discord
        self.lastMessage = time()
        return recipient.receive(self.mind, recipient, message)


    def itergroups(self):
        return iter(self.groups)


    def logout(self):
        for g in self.groups[:]:
            self.leave(g)
