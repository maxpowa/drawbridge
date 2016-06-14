# drawbridge
IRC Gateway for Discord

> Add a readme - RX14 2016

## Installing
```
virtualenv -p python2 venv
source venv/bin/activate

git clone https://github.com/maxpowa/chord.git
cd chord
pip install -e .
cd ..
git clone https://github.com/maxpowa/drawbridge.git
cd drawbridge
pip install -r requirements.txt
```

## Run
`python drawbridge/ircd2.py`

### Usage
Configure a new connection in your irc client using 127.0.0.1:6667 as the address and a server password as follows: `discordemail[/serverid]:discordpassword`. If you prefer token auth, you can set the server password as `token[/serverid]:yourtoken`.

Once connected, you may jump between servers using `/msg Discord jumpserver <id>`.
