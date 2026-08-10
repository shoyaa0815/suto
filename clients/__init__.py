# Each subpackage here is one way of talking to the bot: a chat platform, and
# eventually whatever else drives it. A client's job is only to turn incoming
# messages into a prompt, hand that to ask_local_ai(), and send the answer
# back in whatever shape its platform wants.
#
# Nothing in here holds logic about *what* the bot can do — that lives in ai.py
# and harness/, shared by every client. Adding a client should never mean
# touching them.
