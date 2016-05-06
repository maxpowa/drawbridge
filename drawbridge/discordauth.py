from twisted.internet import defer
from twisted.python import failure
from twisted.cred import error, credentials, checkers

import chord

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


class IDiscordAuth(credentials.ICredentials):
    """
    Required to work properly
    """
    def checkPassword(password):
        """
        Yeah its dumb.
        """


@implementer(IDiscordAuth)
class DiscordAuth:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.token = None
        self.meta = {}

    def set_token(self, token):
        self.token = token
        return defer.succeed(token)

    def set_meta(self, meta):
        self.meta = meta
        return defer.succeed(meta)

    def checkPassword(self, password):
        key = [password]
        if '/' in password:
            key = password.split('/', 1)
        d = self.test_creds(*key)
        d.addCallback(self.set_token)
        d.addCallback(chord.get_user_for_token)
        d.addCallback(self.set_meta)
        return d

    def test_creds(self, *args):
        if len(args) not in [1,2]:
            return failure.Failure(error.UnauthorizedLogin('Unknown login format'))

        print(repr(args))
        if len(args)==1:
            (token,) = args
            return chord.check_token(token)
        if len(args)==2:
            (email, password) = args
            return chord.get_token(email, password)


@implementer(checkers.ICredentialsChecker)
class DiscordChecker(object):
    credentialInterfaces = (IDiscordAuth,)

    def __init__(self):
        self.rate_limit = {}

    def _cbPasswordMatch(self, matched, cred):
        if matched and 'id' in cred.meta:
            return cred.meta['id']
        else:
            return failure.Failure(error.UnauthorizedLogin())

    def rate_limit_middleware(self, credentials):
        if not credentials.username in self.rate_limit:
            # Rate limit to 5 attempts every 5 minutes. We don't wanna get banned from discord.
            self.rate_limit[credentials.username] = TokenBucket(5, float(5/300))
        return self.rate_limit[credentials.username].consume(1)

    def requestAvatarId(self, credentials):
        if self.rate_limit_middleware(credentials):
            return defer.maybeDeferred(
                credentials.checkPassword,
                credentials.password).addCallback(
                self._cbPasswordMatch, credentials)
        else:
            return defer.fail(error.LoginDenied())


class DiscordPortal(object):
    """
    A mediator between clients and a realm.
    """
    def __init__(self, realm, checkers=(DiscordChecker(),)):
        self.realm = realm
        self.checkers = {}
        for checker in checkers:
            self.registerChecker(checker)


    def listCredentialsInterfaces(self):
        return list(self.checkers.keys())


    def registerChecker(self, checker, *credentialInterfaces):
        if not credentialInterfaces:
            credentialInterfaces = checker.credentialInterfaces
        for credentialInterface in credentialInterfaces:
            self.checkers[credentialInterface] = checker


    def login(self, credentials, mind, *interfaces):
        for i in self.checkers:
            if i.providedBy(credentials):
                return defer.maybeDeferred(self.checkers[i].requestAvatarId, credentials
                    ).addCallback(self.realm.requestAvatar, credentials, mind, *interfaces
                    )
        ifac = providedBy(credentials)
        return defer.fail(failure.Failure(error.UnhandledCredentials(
            "No checker for %s" % ', '.join(map(reflect.qual, ifac)))))
