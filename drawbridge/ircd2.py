import sys

from time import ctime

from twisted.cred import error as ecred
from twisted.internet import reactor, protocol, task, defer
from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.python import log
from twisted.python import logfile
from twisted.words import service, iwords, ewords
from twisted.words.protocols import irc
from twisted.logger import LogLevel

from realm import DiscordWordsRealm, User
from auth import DiscordAuthenticator

import chord
from unidecode import unidecode

DISCORD = "Discord!services@discord.gg"

class IRCProtocol(service.IRCUser):

    _authenticator = None
    avatar = None

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

    def connectionMade(self):
        # Nooope, we'll just stop this right here.
        self.irc_PRIVMSG = self.irc_REJECT_PRIVMSG
        self.realm = DiscordWordsRealm('discord.gg')
        self.hostname = self.realm.name
        self._guilds = {}
        self._private_channels = {}
        self._private_channels_by_user = {}

    def connectionLost(self, reason):
        self._guilds = {}
        self._private_channels = {}
        self._private_channels_by_user = {}

        if self.logout is not None:
            self.avatar.disconnect(reason)
            del self.avatar
            defer.maybeDeferred(self.logout)

    @property
    def guilds(self):
        return self._guilds.values()

    def add_guild(self, guild):
        self._guilds[guild.id] = guild

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)

    def remove_guild(self, guild):
        self._guilds.pop(guild.id, None)

    def get_channel(self, id):
        if id is None:
            return None

        for guild in self.guilds:
            channel = guild.get_channel(id)
            if channel is not None:
                return channel

        pm = self.get_private_channel(id)
        if pm is not None:
            return pm

    def get_channel_by_name(self, name):
        raise ValueError('NYI')

    @property
    def private_channels(self):
        return self._private_channels.values()

    def get_private_channel(self, channel_id):
        return self._private_channels.get(channel_id)

    def get_private_channel_by_user(self, user_id):
        return self._private_channels_by_user.get(user_id)

    def add_private_channel(self, channel):
        self._private_channels[channel.id] = channel
        self._private_channels_by_user[channel.user.id] = channel

    def remove_private_channel(self, channel):
        self._private_channels.pop(channel.id, None)
        self._private_channels_by_user.pop(channel.user.id, None)

    def svc_message(self, message):
        self.notice(DISCORD, self.nickname, message)

    def irc_REJECT_PRIVMSG(self, prefix, params):
        """Send a (private) message.
        Parameters: <msgtarget> <text to be sent>
        """
        target = params[0]
        password = params[-1]

        if self.nickname is None:
            self.transport.loseConnection()
        else:
            self.svc_message("Please wait until authentication has completed to send messages.")


    def irc_WHOIS(self, prefix, params):
        """Whois query
        Parameters: [ <target> ] <mask> *( "," <mask> )
        """
        def cbUser(user):
            self.whois(
                self.name,
                user.name, user.id, self.realm.name,
                user.realName, self.realm.name, user.gateway, False,
                0, 0,
                ['#' + group.name for group in user.itergroups()])

        def ebUser(err):
            err.trap(ewords.NoSuchUser)
            self.sendMessage(
                irc.ERR_NOSUCHNICK,
                params[0],
                ":No such nick/channel")

        try:
            user = params[0].decode(self.encoding)
        except UnicodeDecodeError:
            self.sendMessage(
                irc.ERR_NOSUCHNICK,
                params[0],
                ":No such nick/channel")
            return

        self.realm.lookupUser(user).addCallbacks(cbUser, ebUser)

    def irc_NICK(self, prefix, params):
        """Nick message -- Set your nickname.
        Parameters: <nickname> [password]
        [REQUIRED]
        TODO: Update to verify nick updates with Discord
        """
        nickname = params[0]
        try:
            nickname = nickname.decode(self.encoding)
        except UnicodeDecodeError:
            self.svc_message('Your nickname cannot be decoded. Please use ASCII or UTF-8.')
            self.transport.loseConnection()
            return

        if self.password is None and not self.avatar:
            self.svc_message('You must enter your Discord email and password in the Server '
                'Password box of your client. Ensure they are colon separated '
                'as follows: "user@email.com:securePassword".')
            self.transport.loseConnection()
            return

        def wrongPass(failure):
            failure.trap(chord.errors.LoginError)
            self.svc_message('Unable to change nick, are you sure you used the correct password?')

        def rateLimited(failure):
            failure.trap(chord.errors.RateLimitError)
            self.svc_message('Username changes are rate limited to 2 per hour!')

        def onSuccess(*args, **kwargs):
            self.nickname = nickname
            self.name = nickname

        if not self.avatar:
            onSuccess()
            for code, text in self._motdMessages:
                self.sendMessage(code, text.format(**self.factory._serverInfo))
            password = self.password
            self.password = None
            self.logInAs(nickname, password)
        else:
            if len(params) != 2:
                return self.svc_message('NICK must include password. NICK <username> <password>')
            password = params[1]
            d = self.avatar.change_nick(nickname, password)
            d.addCallback(onSuccess)
            d.addErrback(wrongPass)
            d.addErrback(rateLimited)

    def logInAs(self, nickname, password):
        d = self._authenticator.checkPassword(password)
        d.addCallbacks(self._cbLogin, self._ebLogin, errbackArgs=(nickname,))

    def _cbLogin(self, meta):
        # Let them send messages to the world
        del self.irc_PRIVMSG

        nick = unidecode(self._authenticator.meta.get('username')).replace(' ', '_')
        sender = '{}!{}@discord.gg'.format(self.nickname, self._authenticator.meta.get('discriminator'))
        self.sendLine(':{} NICK :{}'.format(sender, nick))
        self.nickname = nick
        self.avatar = User(self.nickname, self._authenticator)
        self.avatar.protocol = self
        self.avatar.loggedIn(self.realm, self)

        self.logout = self._authenticator.logout
        for code, text in self._welcomeMessages:
            self.sendMessage(code, text % self.factory._serverInfo)

    def _ebLogin(self, err, nickname):
        if err.check(ewords.AlreadyLoggedIn):
            self.svc_message("Already logged in. No pod people allowed!")
        elif err.check(ecred.UnauthorizedLogin):
            self.svc_message("Login failed. Goodbye.")
        elif err.check(ecred.LoginDenied):
            self.svc_message("Login denied. You've probably hit the rate limit.")
        elif err.check(chord.errors.LoginError):
            self.svc_message("Unable to login, are you sure you used the correct email and password?")
        else:
            log.msg("Unhandled error during login:")
            log.err(err)
            self.svc_message("Server error during login.  Sorry.")
        self.transport.loseConnection()

    def setAuthenticator(self, authenticator):
        self._authenticator = authenticator

    def irc_unknown(self, prefix, command, params):
        self.sendCommand(irc.ERR_UNKNOWNCOMMAND, (command, ":Unknown command"), self.hostname)


class IRCGateway(protocol.ServerFactory):
    def __init__(self):
        self.realm = None
        self._serverInfo = {
            "serviceName": 'drawbridge',
            "serviceVersion": 'v 0.1',
            "creationDate": ctime()
            }

    def buildProtocol(self, addr):
        p = IRCProtocol()
        p.factory = self
        p.setAuthenticator(DiscordAuthenticator())
        return p


if __name__ == '__main__':
    chord.start_logging(LogLevel.info)
    #log.startLogging(sys.stdout)
    #log.startLogging(logfile.LogFile('out.log', '.', rotateLength=None))

    # Initialize the Cred authentication system used by the IRC server.
    # portal = DiscordPortal(realm)
    #
    # # IRC server factory.
    # ircfactory = IRCGateway(realm, portal)
    # IRC server factory.
    ircfactory = IRCGateway()

    # Connect a server to the TCP port 6667 endpoint and start listening.
    endpoint = TCP4ServerEndpoint(reactor, 6667)
    endpoint.listen(ircfactory)
    reactor.run()
