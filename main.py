from flask import Flask, request
import json
import datetime
from datetime import datetime as dt
from datetime import time as t
import time
from datetime import timedelta
from tigeropen.common.consts import Language, OrderStatus
from tigeropen.push.pb.OrderStatusData_pb2 import OrderStatusData
from tigeropen.push.pb.PositionData_pb2 import PositionData
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.common.util.signature_utils import read_private_key
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import (Market, SecurityType)
from tigeropen.common.util.contract_utils import stock_contract
from tigeropen.common.util.order_utils import (market_order, limit_order)
import asyncio
import math
import logging
import csv
from threading import Thread
import pytz
from tigeropen.quote.quote_client import QuoteClient
from tigeropen.push.pb.AssetData_pb2 import AssetData
from tigeropen.push.push_client import PushClient
from threading import Lock

app = Flask(__name__)
app.logger.disabled = True
log = logging.getLogger('werkzeug')
log.disabled = True

SYMBOLS = {}
STATUS = None
SET_STATUS = None
POSITION = {}
CASH = 0.00  # 现金额
NET_LIQUIDATION = 0.00  # 总价值
order_status = {}  # 订单状态
cash_lock = Lock()

"""
需要的更新：
    1. 将LMT更新为永久GTC 

待处理的问题：
    1. 并发数据同时进入function 无法处理 需要线程排布 -> 更改获取盘口的方式 v2 已修复 
        并发获取数据依然超标 -> 已修复
    2. 盘口转换时会卡住 不再获取数据或交易 -> 已修复
    3. 突然没有信号 -> limiter限制问题 已修复
    4. 出现反向开仓1 -> 问题：status获取次数超标导致下单失败 已修复 
    5. 转换盘会延迟1分钟 -> 更改获取盘口的方式 v3  已修复
                转换依旧存在问题 -> 已修复 提前40秒进入盘口转换 -> 30s -> 20s Testing
    6. 高速订单会存在矛盾行为导致被拒 -> 可以尝试使用回调参数监听成交列表， 
        高买低卖会被拒 -> 已修复 增加连续单判定，如果前一单高买未成交，在提交下一个低卖订单时需要先取消高买订单 
        出现反向开仓2 -> 已修复 原因：持仓数量改变 增加check_position 执行循环持仓检测 双重过滤叠加订单成交导致的数量改变
        
    
对post-hour的更改：
    1. 将盘后改单替换为postHourTradesHandling全自动改单。-> 已完成
    2. 针对盘后快速下跌的价格增加处理方案，防止踩雷。如果价格相比于上一次获取到的价格每分钟下跌了2.5% 且交易量大于1k的情况下再进行预设价格改单 -> 已完成
"""


def after_request(resp):
    resp.headers.pop('Content-Length', None)
    resp.headers['Access-Control-Allow-Origin'] = '52.32.178.7,54.218.53.128,34.212.75.30,52.89.214.238'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, application/json'
    resp.headers['Access-Control-Allow-Methods'] = 'POST'
    return resp


app.after_request(after_request)


def run_asyncio_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        asyncio.gather(
            set_market_status(), get_market_status()
        )
    )


def is_daylight_saving():  # 检查夏令时
    tz = pytz.timezone('America/New_York')
    now = datetime.datetime.now(tz)
    return now.dst() != datetime.timedelta(0)


summer_time = is_daylight_saving()


# 设置只能closing变not yet open 不能反向
async def set_market_status():  # 按照预设时间更新市场状态
    global SET_STATUS, STATUS
    while True:
        try:
            current_time = dt.now()
            if not summer_time:
                current_time += timedelta(hours=1)  # 不是夏令时则加1小时

            current_weekday = current_time.weekday()
            current_only_time = current_time.time()
            sec = 55  # 在这里修改秒
            pre_open = t(17, 58, sec)
            trading_open = t(23, 29, sec)
            post_open = t(5, 59, sec)
            day_close = t(9, 59, 59)  # 固定

            if 0 <= current_weekday <= 4:  # 周一到周五
                if pre_open <= current_only_time <= trading_open:
                    SET_STATUS = "PRE_HOUR_TRADING"
                elif trading_open < current_only_time or current_only_time < post_open:
                    SET_STATUS = "TRADING"
                elif post_open <= current_only_time <= day_close:
                    SET_STATUS = "POST_HOUR_TRADING"
                else:
                    SET_STATUS = "CLOSING"
            elif current_weekday == 5:  # 周六
                if current_only_time < post_open:
                    SET_STATUS = "TRADING"
                elif post_open <= current_only_time <= day_close:
                    SET_STATUS = "POST_HOUR_TRADING"
                else:
                    SET_STATUS = "CLOSING"
            else:  # 周日
                SET_STATUS = "CLOSING"

            transition_times = [pre_open, trading_open, post_open, day_close]
            sleep_time = 60

            for transition_time in transition_times:
                transition_datetime = dt.combine(current_time.date(), transition_time)
                delta_time = transition_datetime - current_time
                if timedelta() < delta_time < timedelta(seconds=10):
                    sleep_time = delta_time.total_seconds() / 4
                    break
                elif timedelta(seconds=11) < delta_time < timedelta(minutes=5):
                    sleep_time = delta_time.total_seconds() / 2
                    break

            if STATUS != SET_STATUS:
                logging.warning("市场状态已经改变，市场状态：%s -> 新市场状态：%s. 时间: %s", STATUS, SET_STATUS,
                                current_time)
                STATUS = SET_STATUS  # 更新全局状态变量

            await asyncio.sleep(max(0.1, sleep_time))

        except Exception as e:
            logging.error(e)
            await asyncio.sleep(10)


async def get_market_status():
    """
    市场校准器，每5分钟运行一次，防止因为未知错误导致市场状态发生不确定的改变
    """
    global STATUS
    while True:
        quote_client = QuoteClient(get_client_config())
        market_status_list = quote_client.get_market_status(Market.US)
        market_status = market_status_list[0]
        check_status = market_status.trading_status
        if STATUS != check_status:
            logging.warning("市场状态出现错误，已校准。市场状态：%s -> 新市场状态：%s. 时间: %s", STATUS, check_status,
                            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
            STATUS = check_status
        await asyncio.sleep(300)


@app.route("/identityCheck", methods=['POST'])
async def identityCheck():
    _data = json.loads(request.data)
    try:
        if 'apiKey' in _data and _data.get('apiKey') == "3f90166a-4cba-4533-86f2-31e690cfabb9":  # 处理交易信号
            print("")
            print("")
            print("============= START =============")
            print("---------- 收到交易信号 -----------")
            if 'action' in _data and 'symbol' in _data and 'price' in _data:
                try:
                    await place_order(_data['action'], _data['symbol'], _data['price'],
                                      _data['percentage'])
                    return {"status": "processing order"}
                except Exception as e:
                    return {"status": "error", "message": str(e)}

        if 'apiKey' in _data and _data.get('apiKey') == "7a8b2c1d-9e0f-4g5h-6i7j-8k9l0m1n2o3p":  # 处理价格信号
            if 'time' in _data and 'symbol' in _data and 'price' in _data and 'volume' in _data:
                try:
                    if STATUS in ["POST_HOUR_TRADING", "PRE_HOUR_TRADING"]:
                        priceAndVolume(_data['time'], _data['symbol'], _data['price'], _data['volume'])
                    return ""
                except Exception as e:
                    return {"status": "error", "message": str(e)}

    except Exception as e:
        logging.warning("出现错误: %s", e)
        return {"status": "error", "message": str(e)}


def priceAndVolume(timestamp, symbol, price, volume):
    SYMBOLS[symbol] = [round(float(price), 2), float(volume), timestamp]


# ------------------------------------------------------------Tiger Client Function--------------------------------------------------------------------------------------------#


def get_client_config():
    client_configs = TigerOpenClientConfig()
    client_configs.private_key = read_private_key('mykey.pem')
    client_configs.tiger_id = '20152364'
    client_configs.account = '20230418022309393'
    client_configs.language = Language.zh_CN
    return client_configs


client_config = get_client_config()
protocol, host, port = client_config.socket_host_port
push_client = PushClient(host, port, use_ssl=(protocol == 'ssl'), use_protobuf=True)


def connect_callback(frame):  # 回调接口 初始化当前Cash/总资产/持仓
    global CASH, NET_LIQUIDATION
    trade_client = TradeClient(client_config)
    portfolio_account = trade_client.get_prime_assets(base_currency='USD')
    CASH = portfolio_account.segments['S'].cash_available_for_trade
    NET_LIQUIDATION = portfolio_account.segments['S'].net_liquidation
    position = trade_client.get_positions(account=client_config.account, sec_type=SecurityType.STK, currency='USD', market=Market.US)
    if len(position) > 0:
        for pos in position:
            POSITION[pos.contract.symbol] = [pos.quantity, 0]
    print('回调系统连接成功, 当前时间:', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), '市场状态：', STATUS)
    print("可用现金额: USD $", CASH)
    print('总资产: USD $', NET_LIQUIDATION)
    print('当前持仓:', POSITION)


def on_asset_changed(frame: AssetData):  # 回调接口 获取实时Cash和总资产
    global CASH, NET_LIQUIDATION
    with cash_lock:
        CASH = frame.cashBalance
        NET_LIQUIDATION = frame.netLiquidation


def on_order_changed(frame: OrderStatusData):  # 回调接口 获取实时订单成交状态
    status_enum = OrderStatus[frame.status]
    order_status[frame.id] = status_enum


def on_position_changed(frame: PositionData):
    """
    POSITION结构
    [symbol][0] = 现有持仓 循环更新
    [symbol][1] = 上次该标的下单的数量
    """
    # 检查symbol是否在POSITION中
    if frame.symbol not in POSITION:
        POSITION[frame.symbol] = [0, 0]  # 初始化为 [现有持仓, 上次下单数量]

    # 更新现有持仓
    POSITION[frame.symbol][0] = frame.position


def start_listening():
    push_client.asset_changed = on_asset_changed
    push_client.order_changed = on_order_changed
    push_client.position_changed = on_position_changed
    push_client.connect(client_config.tiger_id, client_config.private_key)
    push_client.subscribe_asset(account=client_config.account)
    push_client.subscribe_order(account=client_config.account)
    push_client.subscribe_position(account=client_config.account)


async def check_open_order(trade_client, symbol, new_action, new_price, percentage):
    """
    #### 此function用来控制盘后流动性差无法成交的连续订单 在当前一次订单还未成交时更改下一次订单的行为防止出错 ####
    如果订单不存在：break
    如果订单存在：
        旧buy 新sell ->
                    1：旧price高于新price：检测前单是否成交 未成交就直接全部取消 取消后直接在主函数return 取消所有订单订单
                    2：旧price低于新price：股价上升中，挂起等待成交 10秒
        旧buy 新buy -> 不可能
        旧sell 新buy -> 取消买单，放买单进入（长线不可能，因为交易之间会有间隔，但是可以写，以防万一）
        旧sell 新sell -> 取消新订单，数量：旧 -> 旧 + 新, 最大数量为当前持仓

    之后升级为监听函数：使用回调接口获取并记录下已经成交/未成交订单，有新的交易信号来到时进行比对，按照上述逻辑执行。

    :returns
    True -> 无事发生
    False -> 主程序中断新订单
    """
    open_orders = trade_client.get_open_orders(symbol=symbol)  # 检查这个open_orders是否包含部分成交订单
    if not open_orders:
        return True
    order = open_orders[0]
    is_trading_hour = STATUS == "TRADING"
    log_prefix = "[盘中]" if is_trading_hour else "[盘后]"
    if order.action == 'BUY':
        if new_action == 'SELL':
            compare_price = order.latest_price if is_trading_hour else order.limit_price
            if compare_price > new_price:
                trade_client.cancel_order(id=order.id)
                logging.warning("%s %s %s 订单冲突，新旧订单均已取消(1)", log_prefix, symbol, new_action)
                return False
            elif compare_price <= new_price:
                if percentage < 1:
                    quantity = int(abs(percentage - 1) * order.quantity)
                    logging.warning("%s %s %s 新旧订单已合并(2)，数量: %s -> %s", log_prefix, symbol,
                                    new_action, order.quantity, quantity)
                    trade_client.modify_order(order=order, quantity=quantity, limit_price=order.limit_price)
                    return False
                if percentage == 1:
                    trade_client.cancel_order(id=order.id)
                    logging.warning("%s,%s,%s 订单冲突，新旧订单均已取消(3)", log_prefix, symbol,
                                    new_action)
                    return False
        elif new_action == 'BUY':
            return True
    elif order.action == 'SELL' and new_action in {'BUY', 'SELL'}:
        if new_action == 'BUY':
            trade_client.cancel_order(id=order.id)
            logging.warning("%s,%s,%s 订单冲突，旧订单已取消(4)", log_prefix, symbol, new_action)
            return True
        else:
            positions = trade_client.get_positions(account=client_config.account, sec_type=SecurityType.STK,
                                                   currency='USD', market=Market.US, symbol=symbol)
            newQuantity = int(math.ceil(positions[0].quantity * percentage))
            quantity = order.quantity + newQuantity
            if quantity > positions[0].quantity:
                quantity = positions[0].quantity
            trade_client.modify_order(order=order, quantity=quantity, limit_price=new_price)
            logging.warning("%s %s %s 新旧订单已合并(5), 数量: %s -> %s, 价格更改: %s -> %s",
                            log_prefix, symbol,
                            new_action, order.quantity, quantity, order.limit_price, new_price)
            return False


async def place_order(action, symbol, price, percentage=1.00):  # 盘中
    global POSITION
    unfilledPrice = 0
    try:
        trade_client = TradeClient(client_config)
        price = round(float(price), 2)
        action = action.upper()
        percentage = float(percentage)
        order = None

        if not await check_open_order(trade_client, symbol, action, price, percentage):  # 检查当前是否有未成交订单 如果有则挂起等待前一个成交
            return
        max_buy = NET_LIQUIDATION * 0.25
        max_quantity = int(max_buy // price)
        contract = stock_contract(symbol=symbol, currency='USD')

        if STATUS == "TRADING":
            if action == "BUY" and CASH >= max_buy:
                order = market_order(account=client_config.account, contract=contract, action=action,
                                     quantity=max_quantity)

            if action == "BUY" and CASH < max_buy:
                logging.info("[盘中]买入 %s 失败，现金不足，时间：%s", symbol,
                             time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                print("[盘中]买入", symbol, " 失败，现金不足")

            if action == "SELL":
                try:
                    quantity = POSITION[symbol][0] if symbol in POSITION else 0
                    if quantity > 0:
                        POSITION[symbol][1] = quantity  # 本次下单时的持仓数量
                        sellingQuantity = int(math.ceil(quantity * percentage))
                        if sellingQuantity > POSITION[symbol][0] if symbol in POSITION else 0:
                            sellingQuantity = POSITION[symbol][0] if symbol in POSITION else 0
                        order = market_order(account=client_config.account, contract=contract, action=action,
                                             quantity=sellingQuantity)

                    else:
                        print("[盘中] 交易失败，当前没有", symbol, "的持仓")
                        logging.info("[盘中] 交易失败，当前没有 %s 的持仓", symbol)
                        print("============== END ===============")
                        return

                except Exception as e:
                    logging.error("下单中断1，错误原因: %s", e)

            if order:
                try:
                    order_id = trade_client.place_order(order)
                    print("----------------------------------")
                    print("[盘中]标的", symbol, "|", order.action, " 下单成功。Price: $", price, "订单号:", order_id)
                    print("----------------------------------")
                    await asyncio.sleep(10)

                    orders = trade_client.get_order(id=order_id)
                    order_status[order_id] = orders.status
                    record_to_csvTEST(
                        [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), orders.contract.symbol, orders.action,
                         orders.quantity, STATUS])  # test
                    await order_filled(orders, unfilledPrice)

                except Exception as e:
                    logging.error("下单中断2，错误原因: %s", e)
                    print("下单中断2，错误原因:", e)
                    print("订单信息:", order)
                    print("标的", symbol)
            else:
                return

        if STATUS == "POST_HOUR_TRADING" or STATUS == "PRE_HOUR_TRADING":
            if action == "BUY" and CASH >= max_buy:
                order = limit_order(account=client_config.account, contract=contract, action=action,
                                    quantity=max_quantity,
                                    limit_price=round(price, 2))

            if action == "BUY" and CASH < max_buy:
                logging.info("[盘后]买入 %s 失败，现金不足，时间：%s", symbol,
                             time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                print("[盘后]买入", symbol, " 失败，现金不足")

            if action == "SELL":

                try:
                    quantity = POSITION[symbol][0] if symbol in POSITION else 0
                    if quantity > 0:
                        POSITION[symbol][1] = quantity  # 本次下单时的持仓数量
                        sellingQuantity = int(math.ceil(quantity * percentage))
                        if sellingQuantity > POSITION[symbol][0] if symbol in POSITION else 0:
                            sellingQuantity = POSITION[symbol][0] if symbol in POSITION else 0
                        order = market_order(account=client_config.account, contract=contract, action=action,
                                             quantity=sellingQuantity, limit_price=round(price * 0.99995, 2))  # 实盘增加time_in_force = 'GTC'

                    else:
                        print("[盘后] 交易失败，当前没有", symbol, "的持仓")
                        logging.info("[盘后] 交易失败，当前没有 %s 的持仓", symbol)
                        print("============== END ===============")
                        return

                except Exception as e:
                    logging.error("下单中断3，错误原因: %s", e)

            if order:
                try:

                    order_id = trade_client.place_order(order)
                    print("[盘后]标的", symbol, "|", order.action, " 第 1 次下单, 成功。Price: $", price, "订单号:",
                          order_id)

                    orders = trade_client.get_order(id=order_id)
                    order_status[order_id] = orders.status  # 初始化订单状态

                    record_to_csvTEST(
                        [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), orders.contract.symbol, orders.action,
                         orders.quantity, STATUS])
                    sleep_time = 10
                    if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
                        sleep_time = 1
                    await asyncio.sleep(sleep_time)
                    await postHourTradesHandling(trade_client, orders, unfilledPrice)
                except Exception as e:
                    logging.error("下单中断4，错误原因: %s", e)
                    print("下单中断4，错误原因:", e)
                    print("订单信息:", order)
                    print("标的", symbol)
            else:
                return

    except Exception as e:
        logging.error("交易过程中出现错误：%s", str(e))
        raise e


async def check_position(orders):
    quantity = orders.quantity
    if order_status.get(orders.id, None) == OrderStatus.FILLED:  # 优先判断是否成交
        return quantity

    position = POSITION[orders.contract.symbol][0] if orders.contract.symbol in POSITION else 0
    if not position:  # 无持仓
        if orders.action == 'SELL':
            return False  # 跳出改单
        return quantity

    # 有持仓 判断有没有变化
    originalPosition = POSITION[orders.contract.symbol][1] if orders.contract.symbol in POSITION else 0  # 记录仓位

    if POSITION[orders.contract.symbol][0] < originalPosition and orders.action == 'SELL':  # 如果仓位变化了则更新卖出数量
        if quantity > POSITION[orders.contract.symbol][0] if orders.contract.symbol in POSITION else 0:
            quantity = POSITION[orders.contract.symbol][0] if orders.contract.symbol in POSITION else 0
        return quantity
    return quantity  # 不改单


# 实时仓位仓位应该直接去POSITION里面找 更改的仓位才应该被返回来
# 情况1 -> 无持仓 -> 判断是否为卖 卖则取消
# 情况2 -> 仓位改变 -> 判断改变的数量 返回作为改单的新数量 限定不能超过最大值
# 情况3 -> 仓位无变化 -> 返回原来订单数量orders.quantity


async def postHourTradesHandling(trade_client, orders, unfilledPrice):
    trade_attempts = 2
    while True:
        quantity = await check_position(orders)
        if not quantity:
            logging.warning("[出现错误]当前无 %s 持仓", orders.contract.symbol)
            return

        if STATUS == "POST_HOUR_TRADING" or STATUS == "PRE_HOUR_TRADING":
            try:
                if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
                    await order_filled(orders, unfilledPrice)
                    return
                if order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                         OrderStatus.REJECTED] and orders.remaining == orders.quantity:
                    logging.warning(
                        "[订单异常] %s, 标的：%s, 方向：%s, 持仓数量: %s, 实际交易数量：%s, 价格：%s, 时间：%s",
                        orders.reason, orders.contract.symbol, orders.action, POSITION[orders.contract.symbol][0] if orders.contract.symbol in POSITION else 0,
                        orders.quantity,
                        orders.limit_price, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                    return
                else:
                    symbol = orders.contract.symbol
                    if symbol in SYMBOLS:  # 检测标的
                        volume = SYMBOLS[symbol][1]
                        price = SYMBOLS[symbol][0]
                        oldPrice = orders.limit_price
                        if abs(price - oldPrice) <= 0.029:  # 价格变动3分钱以下不改单
                            await asyncio.sleep(10)  # 如果价格没变化 继续等 如果变化了才重新下单
                            continue
                        else:
                            if (oldPrice - price) / oldPrice >= 0.2 and volume >= 1000:
                                price = round(price * 0.992, 2)  # 极端情况改单
                            trade_client.modify_order(orders, limit_price=price, quantity=quantity)
                            logging.warning("[盘后智能改单]标的 %s | %s第 %s 次下单, 成功。Price: $ %s -> $ %s",
                                            orders.contract.symbol, orders.action, trade_attempts,
                                            oldPrice, price)
                            unfilledPrice = price
                            trade_attempts += 1
                            await asyncio.sleep(20)
                            orders = trade_client.get_order(id=orders.id)
                    else:
                        print("[盘后智能改单]标的", orders.contract.symbol, "|", orders.action, "第",
                              trade_attempts,
                              " 次下单，失败。原因：该标的最新价格还未更新")
                        await asyncio.sleep(30)
            except Exception as e:
                logging.warning("[盘后智能改单出现异常]%s, 时间 %s", e,
                                datetime.datetime.fromtimestamp(orders.trade_time / 1000))
                return
        if STATUS == "TRADING":  # 盘前 没改成， 开盘了
            await postToTrading(orders, trade_client, trade_attempts, unfilledPrice)
            break
        if STATUS in ["CLOSING", "NOT_YET_OPEN", "MARKET_CLOSED", "EARLY_CLOSED"]:  # 盘后结束 没改成， 收盘了
            logging.warning("[交易时间超出当日交易时段]已经挂起订单等待盘前后继续交易,标的: %s｜方向: %s｜",
                            orders.contract.symbol,
                            orders.action)
            await asyncio.sleep(28800)


async def order_filled(orders, unfilledPrice):
    """
    测试完毕后在实盘中增加csv算法：在csv记录时对订单进行自动排列以便分析
        在成功交易时将该标的存入一个list中，
        buy：直接记录
        sell：用order.symbol遍历之前成交的列表，寻找已经成交的该标的订单
    """
    priceDiff = None
    priceDiffPercentage = None
    i = 1
    while i < 100:
        if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
            if unfilledPrice != 0:
                priceDiff = round(abs(orders.avg_fill_price - unfilledPrice), 4)
                priceDiffPercentage = round(priceDiff / unfilledPrice * 100, 4)
                logging.warning("｜%s 滑点金额：$%s,｜滑点百分比：%s%%｜", orders.contract.symbol, priceDiff,
                                priceDiffPercentage)
            logging.warning(
                "⬆｜订单成交｜市场状态: %s｜时间: %s｜标的: %s｜方向: %s｜均价: $%s｜佣金: $%s｜总成交额: %s｜数量: %s｜⬆",
                STATUS, datetime.datetime.fromtimestamp(orders.trade_time / 1000), orders.contract.symbol,
                orders.action,
                orders.avg_fill_price, orders.commission, round(orders.filled * orders.avg_fill_price, 2),
                orders.quantity)

            record_to_csv(
                [STATUS, datetime.datetime.fromtimestamp(orders.trade_time / 1000), orders.contract.symbol,
                 orders.action,
                 orders.avg_fill_price, orders.commission, round(orders.filled * orders.avg_fill_price, 2), priceDiff,
                 priceDiffPercentage, orders.id, orders.quantity])

            print("----------------------------------")
            print("订单已成交.成交数量：", orders.filled, "out of", orders.quantity)
            print("订单完成时间: ", datetime.datetime.fromtimestamp(orders.trade_time / 1000))
            print("总价格: USD $", orders.filled * orders.avg_fill_price)
            print("成交均价：USD $", orders.avg_fill_price)
            print("佣金：USD $", orders.commission)
            print("============== END ===============")
            print("")
            print("")

            if orders.id in order_status:
                del order_status[orders.id]
            if orders.quantity == POSITION[orders.contract.symbol][0] == POSITION[orders.contract.symbol][1] and orders.action == 'SELL':
                print('old:', POSITION)
                del POSITION[orders.contract.symbol]
                print('new:', POSITION)
            break

        elif order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED]:
            logging.warning("[订单出错]详情：%s", orders.reason)
            if orders.id in order_status:
                del order_status[orders.id]
            break
        else:
            await asyncio.sleep(5)
            i += 1
    if i == 100:
        logging.warning("[盘中]已经循环等待成交100次，依旧无法成交，请寻找原因。时间：%s, 订单详情: %s, ",
                        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        orders)
        return


def record_to_csvTEST(data):
    try:
        with open('All_Orders_Records.csv', 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(data)
    except Exception as e:
        logging.warning("记录失败：%s", e)


def record_to_csv(data):
    try:
        with open('Completed_Orders_Records.csv', 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(data)
    except Exception as e:
        logging.warning("记录失败：%s", e)


async def postToTrading(orders, trade_client, trade_attempts, unfilledPrice):
    order = market_order(account=client_config.account, contract=orders.contract, action=orders.action,
                         quantity=orders.quantity)
    trade_client.cancel_order(id=orders.id)  # 取消原有的限价单
    orders = trade_client.place_order(order)
    await asyncio.sleep(10)
    while True:
        if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
            logging.warning("[盘中智能改单]标的 %s|%s第 %s 次下单, 成功。修改为市价单类型",
                            orders.contract.symbol,
                            orders.action, trade_attempts)
            await order_filled(orders, unfilledPrice)
            break
        elif order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED]:
            positions = trade_client.get_positions(account=client_config.account, sec_type=SecurityType.STK,
                                                   currency='USD', market=Market.US,
                                                   symbol=orders.contract.symbol)
            logging.warning("[订单异常2]%s, 标的：%s, 方向：%s, 持仓数量: %s, 实际交易数量：%s, 价格：%s, 时间：%s",
                            orders.reason, orders.contract.symbol, orders.action, positions[0].quantity,
                            orders.quantity,
                            orders.limit_price, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
        else:
            await asyncio.sleep(5)  # 等到成交为止


if __name__ == "__main__":
    logging.basicConfig(filename='app.log', level=logging.WARN)
    thread = Thread(target=run_asyncio_loop)
    thread.start()

    push_client.connect_callback = connect_callback
    start_listening()

    app.run('0.0.0.0', 80)