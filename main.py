from flask import Flask, request
import json
from datetime import datetime, time
import datetime
from tigeropen.common.consts import (Language, Market, BarPeriod, QuoteRight, OrderStatus)
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.common.util.signature_utils import read_private_key
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import Market, SecurityType, Currency
from tigeropen.common.util.contract_utils import stock_contract
from tigeropen.common.util.order_utils import (market_order, limit_order, stop_order, stop_limit_order, trail_order,
                                               order_leg)
from tigeropen.quote.quote_client import QuoteClient
import asyncio
import math
import logging
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import csv

app = Flask(__name__)
SYMBOLS = []


def after_request(resp):
    resp.headers.pop('Content-Length', None)
    resp.headers['Access-Control-Allow-Origin'] = '52.32.178.7,54.218.53.128,34.212.75.30,52.89.214.238'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, application/json'
    resp.headers['Access-Control-Allow-Methods'] = 'POST'
    return resp


app.after_request(after_request)


@app.route("/identityCheck", methods=['POST'])
async def identityCheck():
    _data = json.loads(request.data)
    print("")
    print("")
    print("============= START ==============")
    print("------------ 收到数据 -------------")
    try:
        if 'apiKey' in _data and _data.get('apiKey') == "3f90166a-4cba-4533-86f2-31e690cfabb9":  # 处理交易
            print("---------- 身份验证成功 -----------")
            if 'action' in _data and 'symbol' in _data and 'price' in _data:
                try:
                    await placingOrder_in_trade_hours(_data['action'], _data['symbol'], _data['price'],
                                                      _data['percentage'])
                    return {"status": "processing order"}
                except Exception as e:
                    return {"status": "error", "message": str(e)}

        if 'apiKey' in _data and _data.get('apiKey') == "7a8b2c1d-9e0f-4g5h-6i7j-8k9l0m1n2o3p":  # 处理价格信号
            if 'time' in _data and 'symbol' in _data and 'price' in _data and 'volume' in _data:
                print(_data['time'], _data['symbol'], _data['price'], _data['volume'])  # test
                market_status = get_market_status()
                print("市场状态测试：", market_status)  # test
                time_str = _data['time']
                parsed_time = datetime.fromisoformat(time_str.rstrip('Z'))
                print("parsed_time", parsed_time)  # test
                target_time = time(11, 25, 0)
                print("target_time", target_time)  # test
                if market_status in ["POST_HOUR_TRADING", "PRE_HOUR_TRADING"] or (
                        market_status == "TRADING" and parsed_time.time() > target_time):
                    await priceAndVolume(_data['time'], _data['symbol'], _data['price'], _data['volume'])
                    print(SYMBOLS)  # Test
                return ""

    except Exception as e:
        logging.warning("出现错误: %s", e)
        return {"status": "error", "message": str(e)}


# ------------------------------------------------------------Tiger Client Function--------------------------------------------------------------------------------------------#


# 用户配置
def get_client_config():
    client_config = TigerOpenClientConfig()
    client_config.private_key = read_private_key('mykey.pem')
    client_config.tiger_id = '20152364'
    client_config.account = '20230418022309393'
    client_config.language = Language.zh_CN
    return client_config


def get_market_status():
    client_config = get_client_config()
    quote_client = QuoteClient(client_config)
    market_status_list = quote_client.get_market_status(Market.US)  # 获取市场状态
    market_status = market_status_list[0]
    marketStatus = market_status.trading_status
    return marketStatus


async def placingOrder_in_trade_hours(action, symbol, price, percentage=1.00):  # 盘中#
    unfilledPrice = 0
    try:
        client_config = get_client_config()
        trade_client = TradeClient(client_config)
        quote_client = QuoteClient(client_config)
        price = round(float(price), 2)
        action = action.upper()
        percentage = float(percentage)  # tv中，买入也需要设定为1.00，否则会返回空值。或者预先分解空值和数字再输入为参数
        order = None

        # 计算总资产和可用现金
        portfolio_account = trade_client.get_prime_assets(base_currency='USD')
        available_cash = portfolio_account.segments['S'].cash_available_for_trade  # 现金
        total_value = portfolio_account.segments['S'].net_liquidation  # 总资产
        max_buy = total_value * 0.25  # 每层仓位占25%总资产=0.25  记得改回去

        contract = stock_contract(symbol=symbol, currency='USD')

        max_quantity = int(max_buy // price)  # 计算当前金额能购买的最大数量

        market_status_list = quote_client.get_market_status(Market.US)  # 获取市场状态
        market_status = market_status_list[0]
        status = market_status.trading_status

        if market_status.trading_status == "TRADING":  # 盘中
            if action == "BUY" and available_cash >= max_buy:  # 如果是买入且现金大于四分之一仓位才执行
                order = market_order(account=client_config.account, contract=contract, action=action,
                                     quantity=max_quantity)  # 生成订单对象

            if action == "BUY" and available_cash < max_buy:  # 买入失败
                print("==========[盘中]买入", symbol, "失败，当前现金额低于开仓最低需求==========")
                logging.warning("==========[盘中]买入 %S 失败，当前现金额低于开仓最低需求==========", symbol)

            if action == "SELL":  # 卖出计算
                positions = trade_client.get_positions(sec_type=SecurityType.STK, currency='USD', market=Market.US,
                                                       symbol=symbol)  # 计算当前标的持仓

                if len(positions) > 0:
                    if positions[0].quantity > 0:
                        order = market_order(account=client_config.account, contract=contract, action=action,
                                             quantity=int(math.ceil(positions[0].quantity * percentage)))
                else:
                    print("[盘中] 交易失败，当前没有", symbol, "的持仓")
                    logging.warning("[盘中] 交易失败，当前没有 %s 的持仓", symbol)
                    print("============== END ===============")

            if order:  # 提交订单
                try:
                    order_id = trade_client.place_order(order)
                    print("------------ 下单成功!-------------")
                    print("订单号：", order_id)
                    print("标的：", symbol)
                    print("方向：", order.action)
                    print("----------------------------------")

                    await asyncio.sleep(10)  # 盘中波动大 需要比盘后更多时间处理订单
                    orders = trade_client.get_order(id=order_id)
                    order_filled(orders, unfilledPrice, status)  # 检查成交状态

                except Exception as error:
                    logging.error("下单中断，错误原因: %s", error)  # 记录异常信息到日志文件
                    print("下单中断，错误原因:", error)  # 打印异常信息到控制台
                    print("订单信息:", order)
                    print("标的", symbol)

        if market_status.trading_status == "POST_HOUR_TRADING" or market_status.trading_status == "PRE_HOUR_TRADING":
            # 盘前盘后
            if action == "BUY" and available_cash >= max_buy:
                order = limit_order(account=client_config.account, contract=contract, action=action,
                                    quantity=max_quantity,
                                    limit_price=round(price, 2))  # 第一次尝试买入 价格：tv价格

            if action == "BUY" and available_cash < max_buy:  # 买入失败
                print("==========[盘后]卖出", symbol, "失败，当前现金额低于开仓最低需求==========")
                logging.warning("==========[盘后]卖出 %S 失败，当前现金额低于开仓最低需求==========", symbol)

            if action == "SELL":  # 卖出计算
                positions = trade_client.get_positions(sec_type=SecurityType.STK, currency='USD', market=Market.US,
                                                       symbol=symbol)  # 计算当前标的持仓
                if len(positions) > 0:
                    if positions[0].quantity > 0:
                        order = limit_order(account=client_config.account, contract=contract, action=action,
                                            quantity=int(math.ceil(positions[0].quantity * percentage)),
                                            limit_price=round(price * 0.9995, 2))
                else:
                    print("[盘后] 交易失败，当前没有", symbol, "的持仓")
                    logging.warning("[盘后] 交易失败，当前没有 %s 的持仓", symbol)
                    print("============== END ===============")

            if order:  # 提交订单
                try:
                    order_id = trade_client.place_order(order)
                    print("------------ 下单成功!-------------")
                    print("订单号：", order_id)
                    print("标的：", symbol)
                    print("方向：", order.action)
                    print("----------------------------------")
                    await asyncio.sleep(5)
                    orders = trade_client.get_order(id=order.id)
                    await check_filled(trade_client, orders, status)  # 在盘后增加成交检查和追单
                except Exception as error:
                    logging.error("下单中断，错误原因: %s", error)  # 记录异常信息到日志文件
                    print("下单中断，错误原因:", error)  # 打印异常信息到控制台
                    print("订单信息:", order)
                    print("标的", symbol)

    except Exception as e:
        logging.error("交易过程中出现错误：%s", str(e))
        raise e


async def check_filled(trade_client, orders, status):
    unfilledPrice = 0
    if orders.remaining == orders.quantity:  # 没有股票在第一次交易成交
        if orders.status in [OrderStatus.EXPIRED, OrderStatus.CANCELLED, OrderStatus.REJECTED]:  # 检查开仓是否被取消
            logging.warning("订单异常。详情: %s", orders.reason)
            print("订单异常。详情:", orders.reason)
        elif orders.status in [OrderStatus.HELD,
                               OrderStatus.PARTIALLY_FILLED]:  # 订单没有被取消 成交仍为0 尝试修改订单价格继续下单 每一次的新价格为 原始价格 + 0.04%
            if orders.action == "BUY":
                unfilledPrice = orders.limit_price
                print("第一次买入失败，正在进行第二次买入")
                print("价格从$", orders.limit_price, "更改为：$ ", round(orders.limit_price * 1.0015, 2))
                logging.warning("[盘后]B1失败，Price:$ %s -> $ %s", orders.limit_price,
                                round(orders.limit_price * 1.0015, 2))
                trade_client.modify_order(orders, limit_price=round(orders.limit_price * 1.0015, 2))  # 第二次尝试买入
                print("-------[盘后]B2买入下单成功!--------")

                orders = trade_client.get_order(id=orders.id)
                await asyncio.sleep(10)  # 睡十秒
                if orders.remaining == orders.quantity:
                    if orders.status in [OrderStatus.EXPIRED, OrderStatus.CANCELLED, OrderStatus.REJECTED]:
                        logging.warning("订单异常。详情: %s", orders.reason)
                        print("订单异常。详情:", orders.reason)
                    if orders.status == OrderStatus.HELD:
                        # 第三次尝试买入
                        print("第二次买入失败，正在进行第三次买入")
                        print("价格从：$", orders.limit_price, "更改为：$", (round(orders.limit_price * 1.003, 2)))
                        logging.warning("[盘后]B2失败，Price:$ %s -> $ %s", orders.limit_price,
                                        round(orders.limit_price * 1.003, 2))
                        trade_client.modify_order(orders, limit_price=(round(orders.limit_price * 1.003, 2)))
                        print("-------[盘后]B3买入下单成功!--------")

                        orders = trade_client.get_order(id=orders.id)
                        await asyncio.sleep(10)  # 睡十秒
                        if orders.remaining == orders.quantity:
                            if orders.status in [OrderStatus.EXPIRED, OrderStatus.CANCELLED,
                                                 OrderStatus.REJECTED]:
                                logging.warning("订单异常。详情: %s", orders.reason)
                                print("订单异常。详情:", orders.reason)
                            else:
                                trade_client.cancel_order(id=orders.id)  # 实在买不到就算了吧..
                                logging.warning("[盘后]买入 %s 失败，价格涨幅超过改单范围，订单取消。时间： %s",
                                                orders.symbol,
                                                datetime.datetime.fromtimestamp(orders.trade_time / 1000))
                                print("[盘后]买入", orders.symbol, "失败，价格涨幅超过改单范围，订单取消。时间：",
                                      datetime.datetime.fromtimestamp(orders.trade_time / 1000))
                elif (orders.remaining < orders.quantity) and (
                        orders.status in [OrderStatus.HELD, OrderStatus.PARTIALLY_FILLED]):  # 第二次买入已部分成交，不需要再改单
                    print("[盘后]订单B2已成交.成交数量", orders.filled, "out of", orders.quantity)
                    order_filled(orders, unfilledPrice, status)
            if orders.action == "SELL":
                unfilledPrice = orders.limit_price
                print("第一次卖出失败，正在进行第二次卖出")
                print("价格从", orders.limit_price, "更改为：$",
                      round(orders.limit_price - (orders.limit_price * 0.0015), 2))
                logging.warning("[盘后]S1失败，Price: $ %s -> $ %s", orders.limit_price,
                                round(orders.limit_price - (orders.limit_price * 0.0015), 2))
                trade_client.modify_order(orders, limit_price=round(orders.limit_price - (orders.limit_price * 0.0015),
                                                                    2))  # 第二次尝试卖出
                print("-------[盘后]S2卖出下单成功!--------")

                orders = trade_client.get_order(id=orders.id)
                await asyncio.sleep(10)  # 睡十秒
                if orders.remaining == orders.quantity:
                    if orders.status in [OrderStatus.EXPIRED, OrderStatus.CANCELLED, OrderStatus.REJECTED]:
                        logging.warning("订单异常。详情: %s ", orders.reason)
                        print("订单异常。详情:", orders.reason)
                    if orders.status == OrderStatus.HELD:
                        # 第三次尝试卖出
                        print("第二次卖出失败，正在进行第三次卖出")
                        print("价格从", orders.limit_price, "更改为：$",
                              round(orders.limit_price - (orders.limit_price * 0.003), 2))
                        logging.warning("[盘后]S2失败，Price: $ %s -> $ %s", orders.limit_price,
                                        round(orders.limit_price - (orders.limit_price * 0.003), 2))
                        trade_client.modify_order(orders, limit_price=round(
                            orders.limit_price - (orders.limit_price * 0.003), 2))  #
                        print("-------[盘后]S3卖出下单成功!--------")
                        orders = trade_client.get_order(id=orders.id)
                        await asyncio.sleep(10)  # 睡十秒
                        if orders.remaining == orders.quantity:  # 如果走到这一步 问题非常严重 需要立刻邮件/电话/短信/推送 进行实时通知
                            logging.warning("[盘后]卖出 %s 失败，价格跌幅超过预设改单范围，进入自主改单循环。时间： %s",
                                            orders.symbol,
                                            datetime.datetime.fromtimestamp(orders.trade_time / 1000))
                            print("[盘后]卖出", orders.symbol, "失败，价格跌幅超过预设改单范围，进入自主改单循环。时间：",
                                  datetime.datetime.fromtimestamp(orders.trade_time / 1000))
                            await postHourTradesHandling(trade_client, orders, unfilledPrice, status)  # 进入自主改单循环
                elif (orders.remaining < orders.quantity) and (
                        orders.status in [OrderStatus.HELD, OrderStatus.PARTIALLY_FILLED]):  # 第二次卖出已部分成交，不需要再改单
                    order_filled(orders, unfilledPrice, status)
    if orders.remaining < orders.quantity:  # 第一次买入或卖出已经部分成交，不需要再改单
        order_filled(orders, unfilledPrice, status)


# 打印/记录成交详细信息
def order_filled(orders, unfilledPrice, status):
    priceDiff = ""
    priceDiffPercentage = ""
    if unfilledPrice != 0:  # 第一次挂单价格
        priceDiff = round(abs(orders.avg_fill_price - unfilledPrice), 4)
        priceDiffPercentage = round(priceDiff / unfilledPrice * 100, 4)
        logging.warning("｜滑点金额：$%s,｜滑点百分比：%s%%｜", priceDiff,
                        priceDiffPercentage)  # 如果买入卖出第一次未成交将会在这里计算滑点价格 可以用做与后期统计
    logging.warning("⬇｜订单成交｜市场状态: %s｜时间: %s｜标的: %s｜方向: %s｜均价: $%s｜佣金: $%s｜总成交额: %s｜⬇"
                    , status, datetime.datetime.fromtimestamp(orders.trade_time / 1000), orders.contract.symbol,
                    orders.action,
                    orders.avg_fill_price, orders.commission, round(orders.filled * orders.avg_fill_price, 2))
    record_to_csv(
        [status, datetime.datetime.fromtimestamp(orders.trade_time / 1000), orders.contract.symbol, orders.action,
         orders.avg_fill_price, orders.commission, round(orders.filled * orders.avg_fill_price, 2), priceDiff,
         priceDiffPercentage])

    print("订单已成交.成交数量：", orders.filled, "out of", orders.quantity)
    print("订单完成时间: ", datetime.datetime.fromtimestamp(orders.trade_time / 1000))
    print("成交均价：$", orders.avg_fill_price)
    print("佣金：$", orders.commission)
    print("============== END ===============")
    print("")
    print("")


def record_to_csv(data):
    try:
        with open('records.csv', 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(data)
    except Exception as e:
        logging.warning("记录失败：%s", e)


async def priceAndVolume(timestamp, symbol, price, volume):  # 更新tv选定指标的最近k线close价格和最近k线的volume
    for i, sym in enumerate(SYMBOLS):
        if sym[0] == symbol:
            SYMBOLS[i] = [symbol, price, volume, timestamp]  # 如果这个symbol已经在list中了 替换为新数据
            return
    SYMBOLS.append([symbol, price, volume, timestamp])  # 如果这个symbol不在list中 则新建该symbol的list


async def postHourTradesHandling(trade_client, orders, unfilledPrice, market_status):  # 针对盘前盘后不成交的订单提供处理方案
    trade_attempts = 4
    while True:
        if market_status == "POST_HOUR_TRADING" or market_status == "PRE_HOUR_TRADING":
            try:
                if orders.remaining == orders.quantity or (
                        orders.status != OrderStatus.FILLED and orders.remaining < orders.quantity):
                    for i in SYMBOLS:
                        price = round((i[1]), 2)  # 读取收盘价
                        if i[0] == orders.symbol:  # 检测标的是否在list中
                            if i[1] != orders.limit_price:  # 检测新价格价格是否和前一次下单依然一样
                                print("-------[盘后]S", trade_attempts, "卖出下单成功!--------")
                                print("价格从", orders.limit_price, "更改为：$", price)
                                logging.warning("[盘后]S %s 失败，Price: $ %s -> $ %s", trade_attempts,
                                                orders.limit_price, price)
                                trade_client.modify_order(orders, limit_price=price)
                                await asyncio.sleep(20)
                                orders = trade_client.get_order(id=orders.id)
                            else:
                                await asyncio.sleep(20)
                        else:
                            print("下单失败，当前没有该股票价格")
                            logging.warning("标的 %s 第 %s 次下单，失败。原因：还未接收到该标的的最新分钟价格",
                                            orders.symbol, trade_attempts)
                            await asyncio.sleep(20)
                        trade_attempts += 1
                        market_status = get_market_status()  # 1分钟最多获取十次
                if orders.status == OrderStatus.FILLED:
                    order_filled(orders, unfilledPrice, market_status)
                    break
            except Exception as e:
                logging.warning("盘后自动改单出现错误，原因 %s, 时间 %s", e,
                                datetime.datetime.fromtimestamp(orders.trade_time / 1000))
        if market_status == "TRADING":  # 盘前 没改成 开盘了 改成市价单重新下单
            client_config = get_client_config()
            order = market_order(account=client_config.account, contract=orders.contract, action=orders.action,
                                 quantity=orders.quantity)
            orders = trade_client.place_order(order)
            await asyncio.sleep(10)
            while True:
                if orders.remaining < orders.quantity:
                    order_filled(orders, unfilledPrice, market_status)
                    break
                else:
                    await asyncio.sleep(5)
            break
        if market_status in ["CLOSED", "NOT_YET_OPEN", "MARKET_CLOSED", "EARLY_CLOSED"]:  # 盘后结束 没改成 收盘了
            break


"""
问题1：已解决

问题2：无解

问题3：小股手续费太高，需要增加股价阀值，太低价的股票不做，但是这个股票的具体价格定在多少？
思路：在数量相同的情况下，计算不同股票价格(X)的平均每股手续费(Y)曲线方程，得到一个最优点(X,Y)
"""

if __name__ == "__main__":
    logging.basicConfig(filename='app.log', level=logging.WARNING)

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=["500 per day", "50 per hour"],
        storage_uri="memory://",
    )

    app.run('0.0.0.0', 80)
