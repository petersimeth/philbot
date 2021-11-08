"""
Tracks and manages positions.
Sends orders.
https://tda-api.readthedocs.io/en/latest/client.html#orders
"""

from enum import Enum
from datetime import datetime, timedelta
import time

from tda.orders.options import option_buy_to_open_limit, option_sell_to_close_limit, \
option_sell_to_close_market, option_buy_to_open_market
from tda.utils import Utils

from signaler import Signals
from ema import CloudColor, CloudPriceLocation
from botutils import get_std_dev_for_symbol, get_flattened_chain



class StopType(Enum):
    """
    Enum for various stop types.
    Enables dynamic stop levels.
    """
    EMA_LONG, EMA_SHORT = range(2)

    @classmethod
    def stop_type_to_level(cls, stop_type, cloud):
        """
        Gets a number from the type of stop (ie EMA_LONG etc.).
        In case stop_type is a number, return it.
        """
        match stop_type:
            case cls.EMA_SHORT:
                return cloud.short_ema
            case cls.EMA_LONG:
                return cloud.long_ema
            case other:
                return other

    @classmethod
    def stop_tuple_to_level(cls, stop_tuple, cloud):
        """
        Returns a level (price) from a stop tuple.
        stop_tuple expected in the format of:
        (StopType, price_offset: float)
        """
        stop_type, offset = stop_tuple
        return cls.stop_type_to_level(stop_type, cloud) + offset


def level_set(
    current_price, standard_deviation, cloud,
):
    """
    Calculates risk and reward levels.
    Should return a stop loss and take profit levels.
    For opening a new position.

    Returns a stop (in the format (StopType, offset)) and a take profit level.
    """
    stop = None
    take_profit = None
    cloud_color = cloud.status[0]
    cloud_location = cloud.status[1]

    stop_mod = 1  # number of std devs
    take_profit_mod = 2

    direction_mod = 1
    if cloud_color == CloudColor.RED:
        direction_mod = -1

    take_profit_mod = take_profit_mod * direction_mod
    stop_mod = stop_mod * direction_mod

    if cloud_location == CloudPriceLocation.INSIDE:  # ie passing through long ema
        stop = (StopType.EMA_LONG, (standard_deviation * stop_mod * -1))

    # If price passes through short EMA from either color cloud
    if cloud_location in (CloudPriceLocation.ABOVE, CloudPriceLocation.BELOW):
        stop = (StopType.EMA_LONG, 0)
        # or in case the long EMA is very far away
        if abs(cloud.longEMA - current_price) > abs(current_price -
                   (cloud.shortEMA - (direction_mod * 2 * standard_deviation))):
            stop = (StopType.EMA_SHORT, (direction_mod * 2 * standard_deviation))

    riskloss = abs(current_price - StopType.stop_tuple_to_level(stop, cloud))

    take_profit = cloud.shortEMA + (standard_deviation * take_profit_mod)
    # enforce 3:1 reward:risk if take_profit is very far away
    if abs(current_price - take_profit) > 4 * riskloss:
        take_profit = current_price + (direction_mod * 3 * riskloss)

    print(
        f"Take Profit: {take_profit}\nStop Level: {stop}\nStandard Deviation {standard_deviation}")
    return stop, take_profit


class OrderManagerConfig:
    """To hold settings relevant to the OrderManager."""
    def __init__(
        self,
        stdev_period,
        mindte,
        maxdte,
        max_contract_price,
        min_contract_price,
        max_spread,
        max_loss,
        min_loss,
        min_risk_reward_ratio,
        strike_count,
        limit_padding,
        time_btwn_positions,  # this and order_timeout_length in seconds
        order_timeout_length,
    ):
        self.stdev_period = (
            stdev_period  # period of calculation of the standard deviation
        )
        self.mindte = mindte  # days to expiration on the options contracts
        self.maxdte = maxdte
        self.max_contract_price = max_contract_price
        self.min_contract_price = min_contract_price
        self.max_spread = max_spread  # bid/ask spread
        # on price of contract so use option pricing convention ie .10 for 10
        # dollars
        self.max_loss = max_loss
        self.min_loss = min_loss
        # profit/loss expected_move_to_profit/expected_move_to_stop
        self.min_risk_reward_ratio = min_risk_reward_ratio
        self.strike_count = strike_count  # number of strikes to ask the API for
        self.limit_padding = limit_padding  # if set to 0.01 the limit buy will
        # be set at ask+.01
        self.time_btwn_positions = time_btwn_positions
        self.order_timeout_length = order_timeout_length


class Position:
    """
    Controls and tracks an options position
    """

    def __init__(self, contract, take_profit, stop, state):
        self.contract = contract  # contract symbol
        self.state = state  # signaler.Signals.OPEN or OPEN_OR_INCREASE
        # if opened on OPEN_OR_INCREASE only allow position size 1

        self.netpos = 0
        self.associated_orders = {}  # id:status

        self.stop = stop  # (StopType, offset)
        self.take_profit = take_profit

        self.opened_time = datetime.now()
        self.closed_time = None

    # possibly move these into order manager
    # an initializer. for adding to a position use update_position_from_quote

    def open(
        self, client, account_id, limit,
    ):
        """
        For opening a position on the first valid buy signal.

        This method should not be used to add to a position, for
        that use update_position_from_quote and increase.
        """
        while True:
            try:
                response = client.place_order(account_id,
                                              option_buy_to_open_limit(
                                                  self.contract, 1, limit)
                                              .build()
                                              )
                response.raise_for_status()
                break
            except Exception as e:
                print(e)
                time.sleep(0.5)
        order_id = Utils(client, account_id).extract_order_id(response)
        # order_id is potentially None
        if not order_id:
            return 0
        self.associated_orders[order_id] = "OPEN"
        print(f"Order sent for {self.contract}")
        return order_id

    def close(self, client, account_id):
        """
        Cancels any orders not already canceled or filled.
        Sells to close any contracts currently held.
        """
        self.state = Signals.EXIT
        self.closed_time = datetime.now()

        print(f"Closing postion {self.contract}")
        # canceling orders
        for order_id in self.associated_orders:
            if self.associated_orders[order_id] not in {
                    'PENDING_CANCEL', 'CANCELED', 'FILLED', 'REPLACED', 'EXPIRED'}:
                try:
                    client.cancel_order(account_id, order_id)
                except Exception as e:
                    print(
                        f"Exception canceling order "
                        f"(id: {order_id}:{self.associated_orders[order_id]}):\n{e}")
        # selling to close out position (important that this is done
        # after canceling so sell orders don't get canceled)
        if self.netpos < 1:
            return 0

        while True:
            try:
                response = client.place_order(account_id,
                                              option_sell_to_close_market(
                                                  self.contract, self.netpos)
                                              .build()
                                              )
                response.raise_for_status()
                break
            except Exception as e:
                print(e)
                time.sleep(0.5)

        order_id = Utils(client, account_id).extract_order_id(response)
        # order_id is potentially None
        if not order_id:
            return 0
        self.associated_orders[order_id] = "SELL_TO_CLOSE"
        return order_id

    def increase(
        self, client, account_id,
    ):
        """
        Adds to the position
        """
        self.state = Signals.OPEN_OR_INCREASE

        # don't increase if there are open orders
        if "OPEN" in self.associated_orders.values():
            return 0

        while True:
            try:
                response = client.place_order(account_id,
                                              option_buy_to_open_market(
                                                  self.contract, 1,)
                                              .build()
                                              )
                response.raise_for_status()
                break
            except Exception as e:
                print(e)
                time.sleep(0.5)

        order_id = Utils(client, account_id).extract_order_id(response)
        # order_id is potentially None
        if not order_id:
            return 0
        self.associated_orders[order_id] = "OPEN"
        print(f"Adding to position {self.contract}")
        return order_id

    def update_position_from_quote(
            self, cloud, signal, price, standard_deviation, client, account_id):
        """
        Handles stop loss, take profit and adding to a position.
        Opening a position and closing for other reasons
        are handled elsewhere.
        """
        if not price:
            print(self)
            return 0
        if self.state == Signals.EXIT:
            return Signals.EXIT

        if signal == Signals.OPEN_OR_INCREASE and self.state == Signals.OPEN:
            return self.increase(client, account_id)

        cloud_color = cloud.status[0]

        stop_level = StopType.stop_tuple_to_level(self.stop, cloud)
        if (price < stop_level and cloud_color == CloudColor.GREEN) or (
                price > stop_level and cloud_color == CloudColor.RED):
            return self.close(client, account_id)

        if (price > self.take_profit + (standard_deviation * 0.25) and cloud_color == CloudColor.GREEN) or (
                price < self.take_profit - (standard_deviation * 0.25) and cloud_color == CloudColor.RED):
            self.stop = (self.take_profit, 0)
            self.take_profit += (standard_deviation *
                                0.75) if cloud_color == CloudColor.GREEN else (standard_deviation * -0.75)

    def update_from_account_activity(self, message_type, otherdata):
        """
        Handles order status updates like order fills or UROUT messages.
        otherdata argument should be the output of the XML data parser.
        """
        self.associated_orders[otherdata["OrderKey"]] = message_type
        match message_type:
            case "OrderFill":
                original_quantity = int(otherdata["OriginalQuantity"])
                self.netpos += original_quantity if otherdata["OrderInstructions"] == "Buy" else \
                    -1 * original_quantity

    def check_timeouts(self, client, account_id, timeoutlength):
        """
        cancels orders that have been open and unfilled
        for too long.
        """
        now = datetime.now()
        for order_id in self.associated_orders:
            if self.associated_orders[order_id] == "OPEN" and timedelta.total_seconds(
                    now - self.opened_time) < timeoutlength:
                try:
                    client.cancel_order(account_id, order_id)
                except Exception as e:
                    print(
                        f"Exception canceling order (id: {order_id}:{self.associated_orders[order_id]}):\n{e}")


class OrderManager:
    """
    Manages orders and holds relevant data like current positions.
    """
    def __init__(
        self, config,
    ):
        """Initialize OrderManager with an OrderManagerConfig and empty current_positions."""
        self.config = config  # class OrderManagerConfig
        self.current_positions = {}  # symbol:Position

    def updateFromQuote(self, client, account_id, cloud,
                        symbol, signal, newprice):
        """
        Update parameter is the output of
        signaler.update so update should be Signals.something
        or 0.
        """
        # garbage collection
        if symbol in self.current_positions and self.current_positions[symbol].closed_time:
            now = datetime.now()
            if timedelta.total_seconds(
                    now - self.current_positions[symbol].closed_time) > self.config.time_btwn_positions:
                self.current_positions.pop(symbol)
            else:
                return 0
        # this will be a cloud color change
        if signal == Signals.CLOSE and symbol in self.current_positions:
            self.current_positions[symbol].close(client, account_id)

        elif symbol in self.current_positions:
            self.current_positions[symbol].check_timeouts(
                client, account_id, self.config.order_timeout_length)

            standard_deviation = get_std_dev_for_symbol(
                client, symbol, self.config.stdev_period)
            self.current_positions[symbol].update_position_from_quote(
                cloud, signal, newprice, standard_deviation, client, account_id)

        elif signal and signal != Signals.CLOSE:
            self.open_position_from_signal(
                symbol, signal, client, cloud, newprice, account_id,
            )

    def update_from_account_activity(self, symbol, message_type, data):
        """
        Handles new messages from the account activity stream.
        Like order fills or cancels.
        """
        self.current_positions[symbol].update_from_account_activity(
            message_type, data)

    def getContractFromChain(
        self, client, symbol, take_profit, stop, current_price, cloud_color
    ):
        """
        Returns an appropriate options contract symbol.
        should validate risk/reward with the philrate
        """
        putCall = None
        if cloud_color == CloudColor.GREEN:
            putCall = "CALL"
        elif cloud_color == CloudColor.RED:
            putCall = "PUT"

        expected_move_to_profit = abs(take_profit - current_price)
        expected_move_to_stop = abs(stop - current_price)
        contracts = get_flattened_chain(
            client, symbol, self.config.strike_count, self.config.maxdte + 1,
        )
        # contract validation
        contracts = [
            contract
            for contract in contracts
            if contract["ask"] - contract["bid"] <= self.config.max_spread
            and contract["putCall"] == putCall
            and contract["daysToExpiration"] >= self.config.mindte
            and contract["daysToExpiration"] <= self.config.maxdte
            and contract["ask"] > self.config.min_contract_price
            and contract["ask"] < self.config.max_contract_price
        ]

        # risk reward validation
        contracts = [
            contract
            for contract in contracts
            if abs(contract["delta"]) * expected_move_to_stop < self.config.max_loss
            and abs(contract["delta"]) * expected_move_to_stop >= self.config.min_loss
            and expected_move_to_profit / expected_move_to_stop > self.config.min_risk_reward_ratio
        ]

        if contracts:
            # there can only be one
            highest_delta_contract = sorted(
                contracts, key=lambda contract: abs(contract["delta"])
            )[-1]
            return highest_delta_contract
        else:
            return contracts  # None

    def open_position_from_signal(
        self, symbol, signal, client, cloud, price, account_id,
    ):
        """Opens a position based on a signal."""
        standard_dev = get_std_dev_for_symbol(
            client, symbol, self.config.stdev_period)

        stop, take_profit = level_set(price, standard_dev, cloud)
        stop_level = StopType.stop_tuple_to_level(stop, cloud)

        contract = self.getContractFromChain(
            client, symbol, take_profit, stop_level, price, cloud.status[0],
        )
        if not contract:
            print("No suitable contracts")
            return None
        limit = contract["ask"] + self.config.limit_padding

        self.current_positions[symbol] = Position(
            contract["symbol"], take_profit, stop, signal
        )
        self.current_positions[symbol].open(client, account_id, limit)
