#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Import standard library
from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)
from pkg_resources import resource_filename
import datetime
import sys
import logging

# Import modules
import backtrader as bt
import backtrader.feeds as btfeed
import backtrader.analyzers as btanalyzers
import pandas as pd
import numpy as np
from collections.abc import Iterable
import time
from fastquant.notification import trigger_bot
import croniter

from fastquant.config import (
    INIT_CASH,
    COMMISSION_PER_TRANSACTION,
    GLOBAL_PARAMS,
    BUY_PROP,
    SELL_PROP,
    SHORT_MAX,
)

logging.getLogger().setLevel(logging.INFO)


class BaseStrategy(bt.Strategy):
    """
    Base Strategy template for all strategies to be added to fastquant
    """

    # Strategy level arguments
    # After initialization, the `params` variable becomes accessible as an attribute of the strategy object
    # with the properties of a `named tuple`
    params = (
        ("init_cash", INIT_CASH),
        ("buy_prop", BUY_PROP),
        ("sell_prop", SELL_PROP),
        ("commission", COMMISSION_PER_TRANSACTION),
        ("stop_loss", None),
        ("stop_trail", None),
        (
            "execution_type",
            "close",
        ),  # Either open or close, to indicate if a purchase is executed based on the next open or close
        ("periodic_logging", False),
        ("transaction_logging", True),
        ("channel", None),
        ("symbol", None),
        ("allow_short", False),
        ("short_max", SHORT_MAX),
        ("add_cash_amount", None),
        ("add_cash_freq", "M"),
        ("live", True),
        ("to_address", None)
    )

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        logging.info("%s, %s" % (dt.isoformat(), txt))

    def update_order_history(self, order):
        self.order_history["dt"].append(self.datas[0].datetime.date(0))
        self.order_history["type"].append("buy" if order.isbuy() else "sell")
        self.order_history["price"].append(order.executed.price)
        self.order_history["size"].append(order.executed.size)
        self.order_history["value"].append(order.executed.value)
        self.order_history["commission"].append(order.executed.comm)
        self.order_history["pnl"].append(order.executed.pnl)

    def update_periodic_history(self):
        self.periodic_history["dt"].append(self.datas[0].datetime.date(0))
        self.periodic_history["portfolio_value"].append(self.broker.getvalue())
        self.periodic_history["cash"].append(self.broker.getcash())

    def __init__(self):
        # Global variables
        self.init_cash = self.params.init_cash
        self.buy_prop = self.params.buy_prop
        self.sell_prop = self.params.sell_prop
        self.execution_type = self.params.execution_type
        self.periodic_logging = self.params.periodic_logging
        self.transaction_logging = self.params.transaction_logging
        self.commission = self.params.commission
        self.channel = self.params.channel
        self.symbol = self.params.symbol
        self.stop_loss = self.params.stop_loss
        self.stop_trail = self.params.stop_trail
        self.allow_short = self.params.allow_short
        self.short_max = self.params.short_max
        self.live = self.params.live
        self.to_address = self.params.to_address
        self.broker.set_coc(True)
        add_cash_freq = self.params.add_cash_freq

        # Longer term, we plan to add `freq` like notation, similar to pandas datetime
        # https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html
        # M means add cash at the first day of each month
        if add_cash_freq == "M":
            self.add_cash_freq = "0 0 1 * *"
        # W means add cash once a week on monday
        elif add_cash_freq == "W":
            self.add_cash_freq = "0 0 * * 1"
        # Otherwise, it assumes the input is in cron notation (no change)
        else:
            self.add_cash_freq = add_cash_freq

        self.add_cash_amount = self.params.add_cash_amount
        # Attribute that tracks how much cash was added over time
        self.total_cash_added = 0
        print("===Global level arguments===")
        print("init_cash : {}".format(self.init_cash))
        print("buy_prop : {}".format(self.buy_prop))
        print("sell_prop : {}".format(self.sell_prop))
        print("commission : {}".format(self.commission))
        print("stop_loss : {}".format(self.stop_loss))
        print("stop_trail : {}".format(self.stop_trail))
        self.order_history = {
            "dt": [],
            "type": [],
            "price": [],
            "size": [],
            "value": [],
            "commission": [],
            "pnl": [],
        }
        self.periodic_history = {
            "dt": [],
            "portfolio_value": [],
            "cash": [],
        }
        self.order_history_df = None
        self.periodic_history_df = None

        self.dataclose = self.datas[0].close
        self.dataopen = self.datas[0].open

        self.order = None
        self.buyprice = None
        self.buycomm = None
        # Number of ticks in the original input data
        # Note that len(self.data.p.dataname) can still go up later if data is added
        self.len_data = len(self.data.p.dataname)
        # Sets the latest action as "buy", "sell", or "neutral"
        self.action = None

    def buy_signal(self):
        return True

    def sell_signal(self):
        return True

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            # Update order history whenever an order is completed
            self.update_order_history(order)
            if order.isbuy():
                self.action = "buy"
                if self.transaction_logging:
                    self.log(
                        "BUY EXECUTED, Price: %.2f, Cost: %.2f, Comm: %.2f, Size: %.2f"
                        % (
                            order.executed.price,
                            order.executed.value,
                            order.executed.comm,
                            order.executed.size,
                        )
                    )

                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm

            else:  # Sell
                self.action = "sell"
                if self.transaction_logging:
                    self.log(
                        "SELL EXECUTED, Price: %.2f, Cost: %.2f, Comm: %.2f, Size: %.2f"
                        % (
                            order.executed.price,
                            order.executed.value,
                            order.executed.comm,
                            order.executed.size,
                        )
                    )

            self.bar_executed = len(self)

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            if self.transaction_logging:
                if not self.periodic_logging:
                    self.log("Cash %s Value %s" % (self.cash, self.value))
                self.log("Order Canceled/Margin/Rejected")
                self.log("Canceled: {}".format(order.status == order.Canceled))
                self.log("Margin: {}".format(order.status == order.Margin))
                self.log("Rejected: {}".format(order.status == order.Rejected))

        # Write down: no pending order
        self.order = None

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        if self.transaction_logging:
            self.log(
                "OPERATION PROFIT, GROSS: %.2f, NET: %.2f"
                % (trade.pnl, trade.pnlcomm)
            )

    def notify_cashvalue(self, cash, value):
        # Update cash and value every period
        if self.periodic_logging:
            self.log("Cash %s Value %s" % (cash, value))
        self.cash = cash
        self.value = value

    def stop(self):
        # Saving to self so it's accessible later during optimization
        self.final_value = self.broker.getvalue()
        # Note that PnL is the final portfolio value minus the initial cash balance minus the total cash added
        self.pnl = round(self.final_value - self.init_cash - self.total_cash_added, 2)
        print("Final Portfolio Value: {}".format(self.final_value))
        print("Final PnL: {}".format(self.pnl))
        self.order_history_df = pd.DataFrame(self.order_history)
        self.periodic_history_df = pd.DataFrame(self.periodic_history)

        if self.channel:
            trigger_bot(
                self.symbol, self.action, self.current_datetime_str, self.indicators_str,
            )

    def start(self):
        # Used to signal setting the first iteration
        self.first_timepoint = True

    def next(self):
        indicators = self.getindicators()
        indicators_dict = {
            ind.plotlabel()
            if hasattr(ind, "plotlabel")
            else "indicator{}".format(i): ind.lines[0][0]
            for i, ind in enumerate(indicators)
        }
        self.indicators_str = pd.Series(indicators_dict).to_string()
        # Save current datetime string
        self.current_datetime_str = bt.utils.date.num2date(self.data.datetime[0]).strftime("%Y-%m-%dT%H:%M:%S")

        if self.add_cash_amount:
            if self.first_timepoint:
                # Initialize income date iterator, and set next
                start_date = self.datas[0].datetime.datetime(0)
                self.cron = croniter.croniter(self.add_cash_freq, start_date)
                self.next_cash_datetime = self.cron.get_next(datetime.datetime)
                self.log("Start date: {}".format(start_date.strftime("%Y-%m-%d")))
                self.log("Next cash date: {}".format(self.next_cash_datetime.strftime("%Y-%m-%d")))
                
                # Change state to indicate that the cash date iterator has been set
                self.first_timepoint = False

            # Add cash to broker if date is same or later to the next income date
            # This means if the dataset is only for weekdays, a date on a weekend will be executed on the next monday
            if self.datas[0].datetime.datetime(0) >= self.next_cash_datetime:
                self.broker.add_cash(self.add_cash_amount)
                self.next_cash_datetime = self.cron.get_next(datetime.datetime)
                self.total_cash_added += self.add_cash_amount

                self.log("Cash added: {}".format(self.add_cash_amount))
                self.log("Total cash added: {}".format(self.total_cash_added))
                self.log("Next cash date: {}".format(self.next_cash_datetime.strftime("%Y-%m-%d")))

        self.update_periodic_history()
        if self.periodic_logging:
            self.log("Close, %.2f" % self.dataclose[0])
        if self.order:
            return

        if self.periodic_logging:
            self.log("CURRENT POSITION SIZE: {}".format(self.position.size))

        # Skip the last observation since purchases are based on next day closing prices (no value for the last observation)
        # This ends up skipping everything when preload is false so have to cancel when live trading
        if len(self) + 1 >= self.len_data and not self.live:
            return

        # Only sell if you hold least one unit of the stock (and sell only that stock, so no short selling)
        stock_value = self.value - self.cash

        # Only buy if there is enough cash for at least one stock
        if self.buy_signal():
            if self.cash >= self.dataclose[0]:

                if self.transaction_logging:
                    self.log("BUY CREATE, %.2f" % self.dataclose[0])
                # Take a 10% long position every time it's a buy signal (or whatever is afforded by the current cash position)
                # "size" refers to the number of stocks to purchase
                # Afforded size is based on closing price for the current trading day
                # Margin is required for buy commission
                # Add allowance to commission per transaction (avoid margin)
                afforded_size = int(
                    self.cash
                    / (self.dataclose[0] * (1 + self.commission + 0.001))
                )
                buy_prop_size = int(afforded_size * self.buy_prop)
                # Buy based on the closing price of the previous closing day
                if self.execution_type == "close":
                    final_size = min(buy_prop_size, afforded_size)
                    if self.transaction_logging:
                        self.log("Cash: {}".format(self.cash))
                        self.log("Price: {}".format(self.dataclose[0]))
                        self.log("Buy prop size: {}".format(buy_prop_size))
                        self.log("Afforded size: {}".format(afforded_size))
                        self.log("Final size: {}".format(final_size))
                    # Explicitly setting exectype=bt.Order.Close will make the next day's closing the reference price
                    self.order = self.buy(size=final_size)

                    # Implement stop loss at the purchase level (only this specific trade is closed)
                    if self.stop_loss:
                        stop_price = self.data.close[0] * (
                            1.0 - self.stop_loss
                        )
                        self.log("Stop price: {}".format(stop_price))
                        self.sell(
                            exectype=bt.Order.Stop,
                            price=stop_price,
                            size=final_size,
                        )

                    if self.stop_trail:
                        self.log("Stop trail: {}".format(self.stop_trail))
                        self.sell(
                            exectype=bt.Order.StopTrail,
                            trailpercent=self.stop_trail,
                            size=final_size,
                        )

                # Buy based on the opening price of the next closing day (only works "open" data exists in the dataset)
                else:
                    # Margin is required for buy commission
                    afforded_size = int(
                        self.cash
                        / (self.dataopen[1] * (1 + self.commission + 0.001))
                    )
                    final_size = min(buy_prop_size, afforded_size)
                    if self.transaction_logging:
                        self.log("Buy prop size: {}".format(buy_prop_size))
                        self.log("Afforded size: {}".format(afforded_size))
                        self.log("Final size: {}".format(final_size))
                    self.order = self.buy(size=final_size)

                    # Implement stop loss at the purchase level (only this specific trade is closed)
                    if self.stop_loss:
                        stop_price = self.data.close[0] * (
                            1.0 - self.stop_loss
                        )
                        self.log("Stop price: {}".format(stop_price))
                        self.sell(
                            exectype=bt.Order.Stop,
                            price=stop_price,
                            size=final_size,
                        )

                    if self.stop_trail:
                        self.log("Stop trail: {}".format(self.stop_trail))
                        self.sell(
                            exectype=bt.Order.StopTrail,
                            trailpercent=self.stop_trail,
                            size=final_size,
                        )

        elif self.sell_signal():
            if self.allow_short:

                # Sell short based on the closing price of the previous day
                if self.execution_type == "close":
                    if self.live:
                        sell_prop_size = int(
                            SELL_PROP * self.broker.getvalue() / self.dataclose[0]
                        )
                    else:
                        sell_prop_size = int(
                            SELL_PROP * self.broker.getvalue() / self.dataclose[1]
                        )
                    # The max incremental short allowed is the short that would lead to a cumulative short position
                    # equal to the maximum short position (initial cash times the maximum short ratio, which is 1.5 by default)
                    if self.live:
                        max_position_size = max(
                            int(
                                self.broker.getvalue()
                                * self.short_max
                                / self.dataclose[0]
                            )
                            + self.position.size,
                            0,
                        )
                        if max_position_size > 0:
                            if self.transaction_logging:
                                self.log("SELL CREATE, %.2f" % self.dataclose[0])
                            self.order = self.sell(
                                size=min(sell_prop_size, max_position_size)
                            )
                    else:
                        max_position_size = max(
                            int(
                                self.broker.getvalue()
                                * self.short_max
                                / self.dataclose[1]
                            )
                            + self.position.size,
                            0,
                        )
                        if max_position_size > 0:
                            if self.transaction_logging:
                                self.log("SELL CREATE, %.2f" % self.dataclose[1])
                            self.order = self.sell(
                                size=min(sell_prop_size, max_position_size)
                            )

                # Buy based on the opening price of the next closing day (only works "open" data exists in the dataset)
                else:
                    if self.live:
                        sell_prop_size = int(
                            SELL_PROP * self.broker.getvalue() / self.dataopen[0]
                        )
                        # The max incremental short allowed is the short that would lead to a cumulative short position
                        # equal to the maximum short position (initial cash times the maximum short ratio, which is 1.5 by default)
                        max_position_size = max(
                            int(
                                self.broker.getvalue()
                                * self.short_max
                                / self.dataopen[0]
                            )
                            + self.position.size,
                            0,
                        )
                        if max_position_size > 0:
                            if self.transaction_logging:
                                self.log("SELL CREATE, %.2f" % self.dataopen[0])
                            self.order = self.sell(
                                size=min(sell_prop_size, max_position_size)
                            )
                    else:
                        sell_prop_size = int(
                            SELL_PROP * self.broker.getvalue() / self.dataopen[1]
                        )
                        # The max incremental short allowed is the short that would lead to a cumulative short position
                        # equal to the maximum short position (initial cash times the maximum short ratio, which is 1.5 by default)
                        max_position_size = max(
                            int(
                                self.broker.getvalue()
                                * self.short_max
                                / self.dataopen[1]
                            )
                            + self.position.size,
                            0,
                        )
                        if max_position_size > 0:
                            if self.transaction_logging:
                                self.log("SELL CREATE, %.2f" % self.dataopen[1])
                            self.order = self.sell(
                                size=min(sell_prop_size, max_position_size)
                            )

            elif stock_value > 0:
                if self.execution_type == "close":
                    if self.live:
                        if self.transaction_logging:
                            self.log("SELL CREATE, %.2f" % self.dataclose[0])
                        # Sell a 5% sell position (or whatever is afforded by the current stock holding)
                        # "size" refers to the number of stocks to purchase
                        if self.execution_type == "close":
                            if SELL_PROP == 1:
                                self.order = self.sell(
                                    size=self.position.size, exectype=bt.Order.Close
                                )
                            else:
                                # Sell based on the closing price of the previous closing day
                                self.order = self.sell(
                                    size=int(
                                        (stock_value / (self.dataclose[0]))
                                        * self.sell_prop
                                    ),
                                    exectype=bt.Order.Close,
                                )
                    else:
                        if self.transaction_logging:
                            self.log("SELL CREATE, %.2f" % self.dataclose[1])
                        # Sell a 5% sell position (or whatever is afforded by the current stock holding)
                        # "size" refers to the number of stocks to purchase
                        if self.execution_type == "close":
                            if SELL_PROP == 1:
                                self.order = self.sell(
                                    size=self.position.size, exectype=bt.Order.Close
                                )
                            else:
                                # Sell based on the closing price of the previous closing day
                                self.order = self.sell(
                                    size=int(
                                        (stock_value / (self.dataclose[1]))
                                        * self.sell_prop
                                    ),
                                    exectype=bt.Order.Close,
                                )
                else:
                    if self.live:
                        # Sell based on the opening price of the next closing day (only works "open" data exists in the dataset)
                        self.order = self.sell(
                            size=int(
                                (self.init_cash / self.dataopen[0])
                                * self.sell_prop
                            )
                        )
                    else:
                        # Sell based on the opening price of the next closing day (only works "open" data exists in the dataset)
                        self.order = self.sell(
                            size=int(
                                (self.init_cash / self.dataopen[1])
                                * self.sell_prop
                            )
                        )
        else:
            self.action = "neutral"

        if self.data.is_live:
            trigger_bot(
                self.symbol, self.action, self.current_datetime_str, self.indicators_str, channel=self.channel, to_address=self.to_address
            )
