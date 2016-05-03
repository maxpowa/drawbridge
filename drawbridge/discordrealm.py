
from twisted.words import ewords

from twisted.internet import defer
from twisted.python import failure
from twisted.cred import error, credentials, checkers
from twisted.words import service

from zope.interface import implementer

from time import time

class TokenBucket(object):
    """An implementation of the token bucket algorithm.

    >>> bucket = TokenBucket(80, 0.5)
    >>> print bucket.consume(10)
    True
    >>> print bucket.consume(90)
    False
    """
    def __init__(self, tokens, fill_rate):
        """tokens is the total tokens in the bucket. fill_rate is the
        rate in tokens/second that the bucket will be refilled."""
        self.capacity = float(tokens)
        self._tokens = float(tokens)
        self.fill_rate = float(fill_rate)
        self.timestamp = time()

    def consume(self, tokens):
        """Consume tokens from the bucket. Returns True if there were
        sufficient tokens otherwise False."""
        if tokens <= self.tokens:
            self._tokens -= tokens
        else:
            return False
        return True

    def get_tokens(self):
        if self._tokens < self.capacity:
            now = time()
            delta = self.fill_rate * (now - self.timestamp)
            self._tokens = min(self.capacity, self._tokens + delta)
            self.timestamp = now
        return self._tokens
    tokens = property(get_tokens)


class DiscordWordsRealm(service.WordsRealm):
    def __init__(self, *a, **kw):
        super(DiscordWordsRealm, self).__init__(*a, **kw)
        self.users = {}
        self.guilds = {}

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
                    mind.name = avatarId
                    mind.realm = self
                    mind.avatar = avatar
                    return iface, facet, self.logoutFactory(avatar, facet)
            raise NotImplementedError(self, interfaces)

        return self.getUser(avatarId).addCallback(gotAvatar)


    def itergroups(self):
        return defer.succeed(self.guilds.itervalues())

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


    def getUser(self, name):
        assert isinstance(name, unicode)
        if self.createUserOnRequest:
            def ebUser(err):
                err.trap(ewords.DuplicateUser)
                return self.lookupUser(name)
            return self.createUser(name).addErrback(ebUser)
        return self.lookupUser(name)


    def createUser(self, name):
        assert isinstance(name, unicode)
        def cbLookup(user):
            return failure.Failure(ewords.DuplicateUser(name))
        def ebLookup(err):
            err.trap(ewords.NoSuchUser)
            return self.userFactory(name)

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


class IDiscordUser(credentials.ICredentials):
    def checkPassword(password):
        """
        quite obviously required
        """


@implementer(IDiscordUser)
class DiscordUser:
    def __init__(self, username, password, discord):
        self.username = username
        self.password = password
        self.discord = discord

    def checkPassword(self, password):
        key = [password]
        if '/' in password:
            key = password.split('/', 1)
        try:
            self.discord.login(*key)
        except LoginFailure:
            return False
        except HTTPException:
            return False
        except ValueError:
            return False
        return True

@implementer(checkers.ICredentialsChecker)
class DiscordChecker(object):
    credentialInterfaces = (IDiscordUser,)

    def __init__(self):
        self.rate_limit = {}
        self.users = {}


    def addUser(self, username, password):
        raise ValueError('generate stack')
        self.users[username] = password


    def _cbPasswordMatch(self, matched, username):
        if matched:
            return username
        else:
            return failure.Failure(error.UnauthorizedLogin())


    def rate_limit_middleware(self, credentials):
        if not credentials.username in self.rate_limit:
            # Rate limit to 5 attempts every 5 minutes. We don't wanna get banned from discord.
            self.rate_limit[credentials.username] = TokenBucket(5, float(5/300))
        return self.rate_limit[credentials.username].consume(1)


    def requestAvatarId(self, credentials):
        if credentials.username in self.users and self.rate_limit_middleware(credentials):
            return defer.maybeDeferred(
                credentials.checkPassword,
                self.users[credentials.username]).addCallback(
                self._cbPasswordMatch, credentials.username)
        else:
            return defer.fail(error.LoginDenied())
