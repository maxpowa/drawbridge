import sys

from time import ctime

from twisted.cred import error as ecred
from twisted.internet import reactor, protocol, task
from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.python import log
from twisted.words import service, iwords, ewords
from twisted.words.protocols import irc

import discord

from discordrealm import DiscordWordsRealm
from discordauth import DiscordPortal, DiscordAuth


# ROOM = 'room'
# USERS = dict(
#     maxpowa='abc123',
#     user2='pass2',
#     user3='pass3',
#     user4='pass4')

DISCORD = "Discord!services@discord.gg"

class IRCProtocol(service.IRCUser):

    _welcomeMessages = [ (irc.RPL_WELCOME, ":Welcome to %(serviceName)s %(serviceVersion)s, the ugliest Discord bridge in the world.") ]

    _motdMessages = [
        (irc.RPL_MOTDSTART, ":- {serviceName} Message of the Day - "),
        (irc.RPL_MOTD, ":    __                       __          __     __              "),
        (irc.RPL_MOTD, ":.--|  |.----.---.-.--.--.--.|  |--.----.|__|.--|  |.-----.-----."),
        (irc.RPL_MOTD, ":|  _  ||   _|  _  |  |  |  ||  _  |   _||  ||  _  ||  _  |  -__|"),
        (irc.RPL_MOTD, ":|_____||__| |___._|________||_____|__|  |__||_____||___  |_____|"),
        (irc.RPL_MOTD, ":                                                   |_____|      "),
        (irc.RPL_MOTD, ":{serviceVersion:^64}"),
        (irc.RPL_ENDOFMOTD, ":End of /MOTD command.")
        ]

    def process_queue(self):
        if not self.avatar:
            return

        while not self.avatar.message_queue.empty():
            msg = self.avatar.message_queue.get()
            self.sendMessage(*msg)

    def connectionMade(self):
        # Nooope, we'll just stop this right here.
        self.irc_PRIVMSG = self.irc_DISCORD_PRIVMSG
        self.task_id = None
        self.realm = self.factory.realm
        self.hostname = self.realm.name

    def connectionLost(self, reason):
        try:
            if self.task_id is not None:
                self.task_id.stop()
        except AssertionError:
            # Sometimes it will fail because the task has stopped early
            pass

        if self.logout is not None:
            self.logout()
            self.avatar = None

    def irc_DISCORD_PRIVMSG(self, prefix, params):
        """Send a (private) message.
        Parameters: <msgtarget> <text to be sent>
        """
        target = params[0]
        password = params[-1]

        if self.nickname is None:
            self.transport.loseConnection()
        else:
            self.notice(DISCORD, self.nickname,
                "Please wait until authentication has completed to send messages.")

    def irc_NICK(self, prefix, params):
        """Nick message -- Set your nickname.
        Parameters: <nickname>
        [REQUIRED]
        TODO: Update to verify nick updates with Discord
        """
        nickname = params[0]
        try:
            nickname = nickname.decode(self.encoding)
        except UnicodeDecodeError:
            self.notice(DISCORD, nickname,
                'Your nickname cannot be decoded. Please use ASCII or UTF-8.')
            self.transport.loseConnection()
            return

        if self.password is None:
            self.notice(DISCORD, nickname,
                'You must enter your Discord email and password in the Server '
                'Password box of your client. Ensure they are slash separated,'
                ' like the following: "user@email.com/securePassword".')
            self.transport.loseConnection()
            return

        self.nickname = nickname
        self.name = nickname

        for code, text in self._motdMessages:
            self.sendMessage(code, text.format(**self.factory._serverInfo))

        password = self.password
        self.password = None
        self.logInAs(nickname, password)

    def logInAs(self, nickname, password):
        d = self.factory.portal.login(
            DiscordAuth(nickname, password, discord.Client()),
            self,
            iwords.IUser)
        d.addCallbacks(self._cbLogin, self._ebLogin, errbackArgs=(nickname,))

    def _cbLogin(self, (iface, avatar, logout)):
        assert iface is iwords.IUser, "Realm is buggy, got %r" % (iface,)

        print(repr(logout))

        # Let them send messages to the world
        del self.irc_PRIVMSG

        self.avatar = avatar
        self.logout = logout
        for code, text in self._welcomeMessages:
            self.sendMessage(code, text % self.factory._serverInfo)

        # start our message queue as well
        self.task_id = task.LoopingCall(self.process_queue)
        # Run every half a second. This just shows messages from discord to IRC connections
        self.task_id.start(1.0)

    def _ebLogin(self, err, nickname):
        if err.check(ewords.AlreadyLoggedIn):
            self.notice(DISCORD, nickname,
                "Already logged in.  No pod people allowed!")
        elif err.check(ecred.UnauthorizedLogin):
            self.notice(DISCORD, nickname,
                "Login failed.  Goodbye.")
        elif err.check(ecred.LoginDenied):
            self.notice(DISCORD, nickname,
                "Login denied. You've probably hit the rate limit.")
        else:
            log.msg("Unhandled error during login:")
            log.err(err)
            self.notice(DISCORD, nickname,
                "Server error during login.  Sorry.")
        self.transport.loseConnection()

    def irc_unknown(self, prefix, command, params):
        self.sendCommand(irc.ERR_UNKNOWNCOMMAND, (command, ":Unknown command"), self.hostname)


class IRCBridge(protocol.ServerFactory):
    protocol = IRCProtocol

    def __init__(self, realm, portal):
        self.realm = realm
        self.portal = portal
        self._serverInfo = {
            "serviceName": 'drawbridge',
            "serviceVersion": 'v 0.1',
            "creationDate": ctime()
            }

    def buildProtocol(self, addr):
        p = self.protocol()
        p.factory = self
        return p


if __name__ == '__main__':
    log.startLogging(sys.stdout)

    # Initialize the Cred authentication system used by the IRC server.
    realm = DiscordWordsRealm('discord.gg')
    portal = DiscordPortal(realm)

    # IRC server factory.
    ircfactory = IRCBridge(realm, portal)

    # Connect a server to the TCP port 6667 endpoint and start listening.
    endpoint = TCP4ServerEndpoint(reactor, 6667)
    endpoint.listen(ircfactory)
    reactor.run()
