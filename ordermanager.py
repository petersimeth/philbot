# tracks and manages positions
# sends orders
# moves stoploss to take profits
# https://tda-api.readthedocs.io/en/latest/client.html#orders
from signaler import Signals
from enum import Enum


class PositionState(Enum):
    OPEN = 1
    TRAIL_STOP = 2

class LevelSetter:
    """
    calculates risk and reward levels.
    should return a stop loss and a list
    of take profit levels
    """
    pass

class OrderManagerConfig:
    def __init__(self,):
        pass


class Position:
    def __init__(self, contract):
        self.contract = contract  # contract symbol
        self.netpos = 0
        self.associated_orders = {}  # id:status

        se
        f.state = None  # PositionState
        self.stop = None
        self.takeprofit = []

    # possibly move these into order manager
    def open():
        pass

    def close():
        pass

    def increase():
        pass


class OrderManager:
    def __init__(
        self, config,
    ):
        self.config = config  # class OrderManagerConfig
        self.currentpositions = {}  # symbol:Position

    def update(self, symbol, signal, newprice):
        """
        update parameter is the output of
        signaler.update so update should be Signals.something
        or 0
        """
        pass

    def getContractFromChain():
        """
        returns an appropriate options contract symbol
        """
        pass

    def open():
        pass

    def close():
        pass

    def increase():
        pass
