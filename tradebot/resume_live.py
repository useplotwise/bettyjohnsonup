"""Clear a halt on real betting: `python -m tradebot.resume_live`.

The bot halts all real betting when Kalshi's record of a trade doesn't match
what the bot meant to do (wrong side, worse price, or no trade found). Run
this only after a person has looked at what happened. It does not turn real
betting on by itself: DRY_RUN and BOT_BANKROLL still decide that.
"""

from .config import load_settings
from .live import halted, resume_live
from .storage import Store


def main() -> None:
    store = Store(load_settings().db_path)
    reason = halted(store)
    if resume_live(store):
        print(f"Halt cleared. It was: {reason}")
    else:
        print("Real betting was not halted. Nothing to clear.")
    store.close()


if __name__ == "__main__":
    main()
