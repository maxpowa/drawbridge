
from twisted.words import ewords

from twisted.internet import defer
from twisted.python import failure
from twisted.words import service, iwords

import discord

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

    def groupFactory(self, name):
        return Group(name)

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
                    mind.name = avatarId
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
        if self.createGroupOnRequest:
            def ebGroup(err):
                err.trap(ewords.DuplicateGroup)
                return self.lookupGroup(name)
            return self.createGroup(name).addErrback(ebGroup)
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


    def createGroup(self, name):
        assert isinstance(name, unicode)
        def cbLookup(group):
            return failure.Failure(ewords.DuplicateGroup(name))
        def ebLookup(err):
            err.trap(ewords.NoSuchGroup)
            return self.groupFactory(name)

        name = name.lower()
        d = self.lookupGroup(name)
        d.addCallbacks(cbLookup, ebLookup)
        d.addCallback(self.addGroup)
        return d

class Group(object):
    implements(iwords.IGroup)

    def __init__(self, name):
        self.name = name
        self.users = {}
        self.meta = {
            "topic": "",
            "topic_author": "",
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


class User(object):
    implements(iwords.IUser)

    realm = None
    mind = None
    discord = DiscordClient()
    discord_conn = None

    def __init__(self, name, credentials=None):
        self.name = name
        self.groups = []
        self.message_queue = Queue()
        self.lastMessage = time()
        if credentials:
            self.discord.set_token(credentials)
            discord_conn = Thread(target=self.discord.run, args=(False,))
            discord_conn.start()


    def loggedIn(self, realm, mind):
        self.realm = realm
        self.mind = mind
        self.signOn = time()


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
        self.lastMessage = time()
        return recipient.receive(self.mind, recipient, message)


    def itergroups(self):
        return iter(self.groups)


    def logout(self):
        for g in self.groups[:]:
            self.leave(g)


class DiscordClient(discord.Client):
    def set_token(self, credentials):
        self.token = credentials.token
        self.headers = {
            'authorization': self.token,
        }
