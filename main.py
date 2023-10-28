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
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import (Market, SecurityType)
from tigeropen.common.util.contract_utils import stock_contract
from tigeropen.common.util.order_utils import (market_order, limit_order)
import asyncio
import math
import logging
from threading import Thread
import pytz
from tigeropen.quote.quote_client import QuoteClient
from tigeropen.push.pb.AssetData_pb2 import AssetData
from tigeropen.push.push_client import PushClient
from threading import Lock
import os
import smtplib
from email.mime.text import MIMEText
import aiofiles

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
lock_raw_data = asyncio.Lock()
lock_order_record = asyncio.Lock()
lock_filled_order_record = asyncio.Lock()
lock_positions_json = asyncio.Lock()
lock_visualize_record = asyncio.Lock()

my_key = os.environ.get("MY_KEY")
mail = 'joe' + os.environ.get('Email')
mail_password = 'ecmc' + os.environ.get('PAS')

"""
需要的更新：
    1. 将LMT更新为永久GTC 
    2. 将现有订单升级为Queue base的排序系统

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
    resp.headers['Access-Control-Allow-Origin'] = '34.212.75.30,' \
                                                  '52.32.178.7,' \
                                                  '52.89.214.238,' \
                                                  '54.218.53.128'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, application/json'
    resp.headers['Access-Control-Allow-Methods'] = 'POST'
    return resp


app.after_request(after_request)


def run_asyncio_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        asyncio.gather(
            set_market_status(),
            signal_processor()
            # , get_market_status()  # 暂时关闭 减少消耗
        )
    )


def is_daylight_saving():  # 检查夏令时
    tz = pytz.timezone('America/New_York')
    now = datetime.datetime.now(tz)
    return now.dst() != datetime.timedelta(0)


summer_time = is_daylight_saving()


async def set_market_status():  # 按照预设时间更新市场状态
    global SET_STATUS, STATUS
    while True:
        try:
            current_time = dt.now()
            if not summer_time:
                current_time += timedelta(hours=1)  # 不是夏令时则加1小时

            current_weekday = current_time.weekday()
            current_only_time = current_time.time()
            sec = 59  # 在这里修改秒
            pre_open = t(17, 59, sec)
            trading_open = t(23, 29, sec)
            post_open = t(5, 59, sec)
            day_close = t(9, 59, 59)  # 固定
            not_yet_open = t(14, 00, 00)

            if current_weekday == 0:  # 周一
                if current_only_time < not_yet_open:  # 0:00 - 17:59
                    SET_STATUS = "MARKET_CLOSED"
                elif not_yet_open <= current_only_time <= pre_open:  # 18:00 - 17:59
                    SET_STATUS = "NOT_YET_OPEN"
                elif pre_open <= current_only_time <= trading_open:  # 18:00 - 23:29
                    SET_STATUS = "PRE_HOUR_TRADING"
                elif trading_open < current_only_time < t(23, 59, 59):  # 23:30 - 23:59:59
                    SET_STATUS = "TRADING"
            elif 1 <= current_weekday <= 4:  # 周二到周五
                if pre_open <= current_only_time <= trading_open:  # 18:00 - 23:29
                    SET_STATUS = "PRE_HOUR_TRADING"
                elif trading_open < current_only_time or current_only_time < post_open:  # 23:30 - 5:59
                    SET_STATUS = "TRADING"
                elif post_open <= current_only_time < day_close:  # 6:00 - 9:59
                    SET_STATUS = "POST_HOUR_TRADING"
                elif day_close <= current_only_time <= not_yet_open:  # 10:00 - 13:59
                    SET_STATUS = "CLOSING"
                else:
                    SET_STATUS = "NOT_YET_OPEN"  # 14:00 - 17:59
            elif current_weekday == 5:  # 周六
                if current_only_time < post_open:  # 0:00 - 5:59
                    SET_STATUS = "TRADING"
                elif post_open <= current_only_time < day_close:  # 6:00 - 9:59
                    SET_STATUS = "POST_HOUR_TRADING"
                elif day_close <= current_only_time < not_yet_open:  # 10:00 - 13:59
                    SET_STATUS = "CLOSING"
                else:
                    SET_STATUS = "MARKET_CLOSED"  # 14:00 - 23:59
            else:  # 周日
                SET_STATUS = "MARKET_CLOSED"  # 0:00 - 23:59

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


signal_queue = asyncio.Queue()


async def signal_processor():
    orderid = 1
    while True:
        _data = await signal_queue.get()
        try:
            if 'apiKey' in _data and _data.get('apiKey') == "3f90166a-4cba-4533-86f2-31e690cfabb9":  # 处理交易信号
                print("")
                print("")
                print("============= START ==============")
                print("---------- 收到交易信号 ----------")
                if 'action' in _data and 'symbol' in _data and 'price' in _data:
                    try:
                        await place_order(_data['action'], _data['symbol'], _data['price'], orderid,
                                          _data['percentage'])
                        orderid += 1
                    except Exception as e:
                        logging.error(f"处理交易信号时出现错误: {str(e)}")

            if 'apiKey' in _data and _data.get('apiKey') == "7a8b2c1d-9e0f-4g5h-6i7j-8k9l0m1n2o3p":  # 处理价格信号
                if 'time' in _data and 'symbol' in _data and 'price' in _data and 'volume' in _data:
                    try:
                        if STATUS in ["POST_HOUR_TRADING", "PRE_HOUR_TRADING"]:
                            priceAndVolume(_data['time'], _data['symbol'], _data['price'], _data['volume'])
                    except Exception as e:
                        logging.error(f"处理价格信号时出现错误: {str(e)}")
        except Exception as e:
            logging.warning(f"出现错误: {str(e)}")
        await asyncio.sleep(1)


@app.route("/identityCheck", methods=['POST'])
async def identityCheck():
    _data = json.loads(request.data)
    try:
        await signal_queue.put(_data)
        return {"status": "信号已添加到队列"}
    except Exception as e:
        logging.warning(f"将信号添加到队列时出现错误: {str(e)}")
        return {"status": "error", "message": str(e)}


def priceAndVolume(timestamp, symbol, price, volume):
    SYMBOLS[symbol] = [round(float(price), 2), float(volume), timestamp]
    # 需要进一步验证SYMBOL的记录是否成功

# ------------------------------------------------------------Tiger Client Function--------------------------------------------------------------------------------------------#


def get_client_config():
    client_configs = TigerOpenClientConfig()
    client_configs.private_key = my_key
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
    position = trade_client.get_positions(account=client_config.account, sec_type=SecurityType.STK, currency='USD',
                                          market=Market.US)
    if len(position) > 0:
        for pos in position:
            POSITION[pos.contract.symbol] = [pos.quantity, 0]
    if frame:
        print("============================================================================")
        print('回调系统连接成功, 当前时间:', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), '市场状态：', STATUS)
        print("可用现金额: USD $", CASH)
        print('总资产: USD $', NET_LIQUIDATION)
        if not POSITION:
            print('当前持仓: 🈚️️')
        else:
            print('当前持仓:', POSITION)
        print("============================================================================")


def on_asset_changed(frame: AssetData):  # 回调接口 获取实时Cash和总资产
    global CASH, NET_LIQUIDATION
    with cash_lock:
        CASH = frame.cashBalance
        NET_LIQUIDATION = frame.netLiquidation


def on_order_changed(frame: OrderStatusData):  # 回调接口 获取实时订单成交状态
    status_enum = OrderStatus[frame.status]
    order_status[frame.id] = status_enum


def on_position_changed(frame: PositionData):  # 回调接口 获取实时持仓
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


async def check_open_order(trade_client, symbol, new_action, new_price, percentage, orderid):
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

    之后升级为监听函数：使用回调接口获取并记录下已经成交/未成交订单，有新的交易信号来到时进行比对，按照上述逻辑执行。 -> 已完成

    :returns
    True -> 无事发生
    False -> 主程序中断新订单
    order -> 取消的订单 检查状态

    logging格式均为[方向][数量][价格]

    因为市价单盘中无法获取实时价格，导致order对象不存在order.limit_price参数，
    在模拟中使用1分钟线会出现盘中下单过快导致和有关limit_price的操作全部异常。 -> 已解决 根据盘口创建判断条件
    """
    logging.info("订单编号|%s|检查重复订单中", orderid)
    open_orders = trade_client.get_open_orders(symbol=symbol)
    if not open_orders:
        return True, None, None
    order = open_orders[0]
    is_trading_hour = STATUS == "TRADING"
    log_prefix = "[盘中]" if is_trading_hour else "[盘后]"

    holding = POSITION[symbol][0] if symbol in POSITION else 0  # 获取标的现有持仓
    sellingQuantity = int(math.ceil(holding * percentage))  # 计算新订单卖出数量
    if sellingQuantity > POSITION[symbol][0] if symbol in POSITION else 0:
        sellingQuantity = POSITION[symbol][0] if symbol in POSITION else 0

    compare_price = order.latest_price if is_trading_hour else order.limit_price
    old_order_price = compare_price  # 以市价单下单时的价格作为输出
    if not is_trading_hour:
        old_order_price = order.limit_price

    if order.action == 'BUY':
        if new_action == 'SELL':
            if compare_price > new_price:  # 1 取消两个订单
                trade_client.cancel_order(id=order.id)
                logging.warning(
                    "%s, %s, 旧订单%s, %s, %s与新进请求%s, %s, %s冲突，两个订单均被取消. 时间: %s, ref = (1)",
                    log_prefix, symbol, order.action, order.quantity, old_order_price, new_action, sellingQuantity,
                    new_price, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                return False, order, 'CANCEL'

            elif compare_price <= new_price:
                if percentage < 1:  # 2 仅改变数量
                    quantity = int(abs(percentage - 1) * order.quantity)
                    logging.warning(
                        "%s, %s, 旧订单%s, %s, %s与新进请求%s, %s, %s冲突，已合并为新订单->%s, %s, %s. 时间: %s, ref = (2)",
                        log_prefix, symbol,
                        order.action, order.quantity, old_order_price,
                        new_action, sellingQuantity, new_price,
                        order.action, quantity, old_order_price,
                        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                    if is_trading_hour:
                        trade_client.modify_order(order=order, quantity=quantity)
                    if not is_trading_hour:
                        trade_client.modify_order(order=order, quantity=quantity, limit_price=order.limit_price)
                    return False, order, 'MODIFY'

                if percentage == 1:  # 3 取消两个订单
                    trade_client.cancel_order(id=order.id)
                    logging.warning(
                        "%s, %s, 旧订单%s, %s, %s与新进请求%s, %s, %s冲突，两个订单均被取消. 时间: %s, ref = (3)",
                        log_prefix, symbol, order.action, order.quantity, old_order_price, new_action, sellingQuantity,
                        new_price, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                    return False, order, 'CANCEL'

        elif new_action == 'BUY':
            return True, None, None  # 只有 buy->buy 返回None

    elif order.action == 'SELL' and new_action in {'BUY', 'SELL'}:
        if new_action == 'BUY':  # 4 仅取消旧订单
            trade_client.cancel_order(id=order.id)
            quantity = int((NET_LIQUIDATION * 0.25) // new_price)
            logging.warning(
                "%s, %s, 旧订单%s, %s, %s与新进请求%s, %s, %s冲突，旧订单已被取消. 时间: %s, ref = (4)",
                log_prefix, symbol, order.action, order.quantity, old_order_price, new_action, quantity,
                new_price, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
            return True, order, 'CANCEL'
        else:  # 5 仅修改订单，不取消
            quantity = order.quantity + sellingQuantity
            if quantity > POSITION[symbol][0] if symbol in POSITION else 0:
                quantity = POSITION[symbol][0] if symbol in POSITION else 0
            if is_trading_hour:
                trade_client.modify_order(order=order, quantity=quantity)
            if not is_trading_hour:
                trade_client.modify_order(order=order, quantity=quantity, limit_price=new_price)
            logging.warning(
                "%s, %s, 旧订单%s, %s, %s与新进请求%s, %s, %s冲突，已合并为新订单->%s, %s, %s. 时间: %s, ref = (5)",
                log_prefix, symbol,
                order.action, order.quantity, old_order_price,
                new_action, sellingQuantity, new_price,
                order.action, quantity, new_price,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
            return False, order, 'MODIFY'


async def place_order(action, symbol, price, orderid, percentage=1.00):  # 盘中
    logging.info("==============订单编号|%s|订单生成中==============", orderid)
    global POSITION
    unfilledPrice = 0
    trade_client = TradeClient(client_config)
    price = round(float(price), 2)
    action = action.upper()
    percentage = float(percentage)
    order = None
    holds = '否'

    logging.info("订单编号|%s|订单基础信息%s, %s, %s, %s, %s", symbol, action, price, percentage,
                 time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
    if symbol in list(SYMBOLS.keys()):
        holds = '是'
    cash_percentage = str(100 * round((NET_LIQUIDATION - CASH) / NET_LIQUIDATION, 2)) + '%'
    logging.info("订单编号|%s|当前可用金额百分比: %s, 是否持有当前标的: %s", orderid, cash_percentage, holds)

    checker, old_order, identifier = await check_open_order(trade_client, symbol, action, price, percentage, orderid)
    if old_order:  # 如果有订单回传则检查其状态 必须是取消才能下一步 避免订单冲突
        if identifier == 'CANCEL':
            i = 0
            while old_order.status != OrderStatus.CANCELLED:
                old_order = trade_client.get_order(id=old_order.id)
                if i == 60:
                    logging.warning("旧订单取消出现问题%s", old_order)
                    return
                await asyncio.sleep(1)
                i += 1
            logging.warning("旧订单已取消，%s %s,订单最后更新时间: %s, 下单时间: %s, 订单号:%s", old_order.action,
                            old_order.quantity, datetime.datetime.fromtimestamp(old_order.update_time / 1000),
                            datetime.datetime.fromtimestamp(old_order.order_time / 1000), old_order.id)
        if identifier == 'MODIFY':
            if STATUS == "TRADING":
                await order_filled(old_order, unfilledPrice)
            else:
                await postHourTradesHandling(trade_client, old_order, old_order.price)
            return

    if not checker:
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
            return

        if action == "SELL":
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
                logging.info("订单编号|%s|买入失败，当前没有 %s 的持仓", orderid, symbol)
                print("============== END ===============")
                return

        if order:
            order_id_main = trade_client.place_order(order)
            print("----------------------------------")
            print("[盘中]标的", symbol, "|", order.action, " 下单成功。\n\rPrice: $", price, "订单:", orderid)
            print("----------------------------------")
            orders = trade_client.get_order(id=order_id_main)
            orders.user_mark = orderid
            order_status[order_id_main] = orders.status
            await record_to_csvTEST(
                [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), orders.contract.symbol, orders.action, price,
                 orders.quantity, orders.id, orderid])  # test

            sleep_time = 10
            if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
                sleep_time = 1
            await asyncio.sleep(sleep_time)
            await order_filled(orders, unfilledPrice)
        else:
            logging.warning("订单为空, 时间: %s, 标的: %s, Action: %s, Price: %s, Percentage: %s",
                            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), symbol, action, price, percentage)
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
            return

        if action == "SELL":
            quantity = POSITION[symbol][0] if symbol in POSITION else 0
            if quantity > 0:
                POSITION[symbol][1] = quantity  # 本次下单时的持仓数量
                sellingQuantity = int(math.ceil(quantity * percentage))
                if sellingQuantity > POSITION[symbol][0] if symbol in POSITION else 0:
                    sellingQuantity = POSITION[symbol][0] if symbol in POSITION else 0
                order = limit_order(account=client_config.account, contract=contract, action=action,
                                    quantity=sellingQuantity,
                                    limit_price=round(price * 0.99995, 2))  # 实盘增加time_in_force = 'GTC'

            else:
                print("[盘后] 交易失败，当前没有", symbol, "的持仓")
                logging.info("订单编号|%s|买入失败，当前没有 %s 的持仓", orderid, symbol)
                print("============== END ===============")
                return

        if order:
            order_id_main = trade_client.place_order(order)
            print("[盘后]标的", symbol, "|", order.action, " 第 1 次下单, 成功。\n\rPrice: $", price, "订单号:", orderid)

            orders = trade_client.get_order(id=order_id_main)
            order_status[order_id_main] = orders.status  # 初始化订单状态
            orders.user_mark = orderid

            await record_to_csvTEST(
                [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), orders.contract.symbol, orders.action, price,
                 orders.quantity, orders.id, orderid])
            sleep_time = 10
            if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
                sleep_time = 1
            await asyncio.sleep(sleep_time)
            await postHourTradesHandling(trade_client, orders, unfilledPrice)
        else:
            logging.warning("订单为空, 时间: %s, 标的: %s, Action: %s, Price: %s, Percentage: %s",
                            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), symbol, action, price, percentage)
            return


async def check_position(orders):
    """
    # 实时仓位仓位应该直接去POSITION里面找 更改的仓位才应该被返回来
    # 情况1 -> 无持仓 -> 判断是否为卖 卖则取消
    # 情况2 -> 仓位改变 -> 判断改变的数量 返回作为改单的新数量 限定不能超过最大值
    # 情况3 -> 仓位无变化 -> 返回原来订单数量orders.quantity
    """
    logging.info("订单编号|%s|check point 0", orders.user_mark)
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


async def postHourTradesHandling(trade_client, orders, unfilledPrice):
    """
    当前问题： 总在0123循环 无法进入4
    """
    logging.info("订单编号|%s|进行盘后交易循环", orders.user_mark)
    trade_attempts = 2
    initial_price = orders.limit_price
    checker = 0
    while True:
        quantity = await check_position(orders)
        logging.info("订单编号|%s|check point 1", orders.user_mark)
        if not quantity:
            logging.warning("[出现错误]当前无 %s 持仓", orders.contract.symbol)
            return

        if STATUS == "POST_HOUR_TRADING" or STATUS == "PRE_HOUR_TRADING":
            logging.info("订单编号|%s|check point 2", orders.user_mark)
            if not orders.remaining and (
                    order_status.get(orders.id, None) == OrderStatus.FILLED or orders.status == OrderStatus.FILLED):
                await order_filled(orders, unfilledPrice)
                return
            elif (order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                        OrderStatus.REJECTED]) and orders.remaining == orders.quantity and not orders.filled > 0 and (
                    orders.reason not in ['改单成功', '', None]):
                logging.warning(
                    "[订单%|%s|异常] %s, 标的：%s, 方向：%s, 持仓数量: %s, 实际交易数量：%s, 价格：%s, 时间：%s", orders.id,
                    orders.reason, orders.contract.symbol, orders.action,
                    POSITION[orders.contract.symbol][0] if orders.contract.symbol in POSITION else 0,
                    orders.quantity,
                    orders.limit_price, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                return
            else:
                logging.info("订单编号|%s|check point 3", orders.user_mark)
                symbol = orders.contract.symbol
                if symbol in SYMBOLS:  # 检测标的
                    volume = SYMBOLS[symbol][1]
                    price = SYMBOLS[symbol][0]
                    oldPrice = orders.limit_price
                    logging.info("订单编号|%s|check point 4", orders.user_mark)
                    if abs(price - oldPrice) <= 0.029:  # 价格变动3分钱以下不改单
                        await asyncio.sleep(10)  # 如果价格没变化 继续等 如果变化了才重新下单
                        continue
                    else:
                        if (oldPrice - price) / oldPrice >= 0.2 and volume >= 1000:
                            price = round(price * 0.992, 2)  # 极端情况改单
                            send_email(orders.contract.symbol, orders.action, orders.quantity, initial_price)
                        orders = trade_client.get_order(id=orders.id)
                        trade_client.modify_order(orders, limit_price=price, quantity=quantity)
                        logging.warning("[盘后智能改单]订单|%s|标的 %s | %s第 %s 次下单, 成功。Price: $ %s -> $ %s",
                                        orders.id,
                                        orders.contract.symbol, orders.action, trade_attempts,
                                        oldPrice, price)
                        unfilledPrice = price
                        trade_attempts += 1
                        await asyncio.sleep(20)
                        orders = trade_client.get_order(id=orders.id)
                        logging.info("订单编号|%s|check point 5", orders.user_mark)
                else:
                    if trade_attempts == 2 and not checker:
                        checker = 1
                        logging.info("订单编号|%s|自动改单失败，当前标价格还未更新", orders.user_mark)
                        await asyncio.sleep(30)
                        continue
                    symbol_list = list(SYMBOLS.keys())
                    logging.info("订单编号|%s|自动改单报错，SYMBOLS：%s 不存在当前标的: %s,", orders.user_mark, symbol_list, symbol)
                    await asyncio.sleep(30)
                    continue
        if STATUS == "TRADING":  # 盘前 没改成， 开盘了
            await postToTrading(orders, trade_client, unfilledPrice)
            break
        if STATUS in ["CLOSING", "NOT_YET_OPEN", "MARKET_CLOSED",
                      "EARLY_CLOSED"]:  # 盘后结束 没改成，收盘了  之后使用GTC 更改逻辑为内循环检查开盘状态 开盘后重新进入post hour订单大循环
            logging.warning("[交易时间超出当日交易时段]已经挂起订单|%s|等待盘前后继续交易,标的: %s｜方向: %s｜",
                            orders.user_mark,
                            orders.contract.symbol,
                            orders.action)
            await asyncio.sleep(28800)


async def order_filled(orders, unfilledPrice):
    """
    应该创建一个逻辑流 所有订单都永久挂单 直到成交
    针对盘中不成交处理 如果没有办法成交则在盘后用实时价格转为限价单 (待处理)
    is_trading_hour = STATUS == "TRADING"
    """
    logging.info("订单编号|%s|交易进入结束环节", orders.user_mark)
    priceDiff = None
    priceDiffPercentage = None
    trade_client = TradeClient(client_config)
    i = 1
    while True:
        try:
            if i >= 20:
                orders = trade_client.get_order(id=orders.id)
                if order_status.get(orders.id, None) != orders.status:
                    order_status[orders.id] = orders.status  # 每分钟手动校准一次订单状态
                    logging.warning("[订单状态校准]订单|%s|状态校准成功. %s, 时间： %s", orders.user_mark, orders,
                                    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                i = 1
                continue
            if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
                if unfilledPrice:
                    priceDiff = round(abs(orders.avg_fill_price - unfilledPrice), 4)
                    priceDiffPercentage = round(priceDiff / unfilledPrice * 100, 4)
                    logging.warning("｜%s 滑点金额：$%s,｜滑点百分比：%s%%｜", orders.contract.symbol, priceDiff,
                                    priceDiffPercentage)
                if not priceDiff:
                    priceDiff = ''
                    priceDiffPercentage = ''

                logging.warning(
                    "订单|%s|%s｜标的: %s｜方向: %s｜数量: %s｜均价: $%s｜佣金: $%s｜成交额: %s｜市场状态: %s｜时间: %s｜⬆",
                    orders.user_mark, orders.id,
                    orders.contract.symbol, orders.action, orders.quantity, orders.avg_fill_price, orders.commission,
                    round(orders.filled * orders.avg_fill_price, 2), STATUS,
                    datetime.datetime.fromtimestamp(orders.trade_time / 1000))

                data = [orders.contract.symbol, orders.action, orders.quantity, orders.avg_fill_price,
                        orders.commission,
                        round(orders.filled * orders.avg_fill_price, 2), STATUS,
                        datetime.datetime.fromtimestamp(orders.trade_time / 1000), orders.id, priceDiff,
                        priceDiffPercentage]

                await csv_visualize_data(data)
                await record_to_csv(data + [orders.user_mark])

                print("----------------------------------")
                print("订单已成交.成交数量：", orders.filled, "out of", orders.quantity)
                print("订单完成时间: ", datetime.datetime.fromtimestamp(orders.trade_time / 1000))
                print("总成交额: USD $", orders.filled * orders.avg_fill_price)
                print("成交均价：USD $", orders.avg_fill_price)
                print("佣金：USD $", orders.commission)
                print("============== END ===============")
                print("")
                print("")

                if orders.id in order_status:
                    del order_status[orders.id]
                if orders.quantity == POSITION[orders.contract.symbol][0] == \
                        POSITION[orders.contract.symbol][1] and orders.action == 'SELL':
                    del POSITION[orders.contract.symbol]
                logging.info("==============订单编号|%s|订单已结束==============", orders.user_mark)
                return

            elif order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                       OrderStatus.REJECTED]:
                logging.warning("[订单|%s|出错]%s", orders.user_mark, orders)
                if orders.id in order_status:
                    del order_status[orders.id]
                return
            else:
                await asyncio.sleep(3)
                i += 1
                orders = orders.get_order(id=orders.id)
        except Exception as e:
            logging.warning("[Order_fill过程中出现问题]订单|%s|详情:%s, \n\r错误详情 %s", orders.user_mark, orders, e)
            return


async def postToTrading(orders, trade_client, unfilledPrice):
    logging.info("订单编号|%s|从post进入trading，订单类型改变中", orders.user_mark)
    order = market_order(account=client_config.account, contract=orders.contract, action=orders.action,
                         quantity=orders.quantity)
    trade_client.cancel_order(id=orders.id)  # 取消原有的限价单
    orders = trade_client.place_order(order)
    await asyncio.sleep(10)
    while True:
        if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
            await order_filled(orders, unfilledPrice)
        else:
            await asyncio.sleep(5)  # 等到成交为止


# ------------------------------------------------------------ CSV算法 / 邮件功能 --------------------------------------------------------------------------------------------------------#

'''
查错机制 & 各文件解释：
TV端 -> 检查真实信号原
所有收到订单raw_data.csv -> 有订单则可以排除tv端问题  此外可以检查订单percentage
创建order记录.csv -> 有订单则排除创建order之前的问题
app.log -> 成交后的记录 检查成交之后 记录到csv之前的问题
已成交订单记录.csv -> 查看与老虎端是否相符 相符则真实成交
老虎端查询 -> 确认真实成交
'''


async def record_to_csvTEST2(data):
    async with lock_raw_data:  # 当前未使用 可留作备用
        try:
            async with aiofiles.open('所有收到订单raw_data.csv', 'a', newline='', encoding='utf-8') as csvfile:
                await csvfile.write(','.join(map(str, data)) + '\n')
        except Exception as e:
            logging.warning("记录失败：%s", e)


async def record_to_csvTEST(data):
    async with lock_order_record:
        try:
            async with aiofiles.open('创建order记录.csv', 'a', newline='', encoding='utf-8') as csvfile:
                await csvfile.write(','.join(map(str, data)) + '\n')
        except Exception as e:
            logging.warning("记录失败：%s", e)


async def record_to_csv(data):
    async with lock_filled_order_record:
        try:
            async with aiofiles.open('已成交订单记录.csv', 'a', newline='', encoding='utf-8') as csvfile:
                await csvfile.write(','.join(map(str, data)) + '\n')
        except Exception as e:
            logging.warning("记录失败：%s", e)


async def load_positions():
    async with lock_positions_json:
        try:
            async with aiofiles.open('持仓.json', 'r', encoding='utf-8') as f:
                data = await f.read()
                return json.loads(data)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


async def save_positions(positions):
    async with lock_positions_json:
        async with aiofiles.open('持仓.json', 'w', encoding='utf-8') as f:
            await f.write(json.dumps(positions))


async def csv_visualize_data(record):
    try:
        positions = await load_positions()

        ticker, action, quantity, avg_fill_price, commission, total_price, status, trade_time, _id, priceDiff, priceDiffPercentage = record
        if isinstance(trade_time, int):
            trade_time = datetime.datetime.fromtimestamp(trade_time / 1000)
        avg_fill_price = float(avg_fill_price)

        if ticker not in positions:
            positions[ticker] = {
                'buy_time': 0,
                'buy_price': 0,
                'sell_prices': [],
                'quantity': 0,
                'init_quantity': quantity,
                'commission': 0
            }

        if action == "BUY":
            positions[ticker]['buy_time'] = str(trade_time)
            positions[ticker]['buy_price'] = avg_fill_price
            positions[ticker]['quantity'] += quantity
            positions[ticker]['commission'] += commission

        elif action == "SELL":
            positions[ticker]['sell_prices'].append(avg_fill_price)
            positions[ticker]['quantity'] -= quantity
            positions[ticker]['commission'] += commission

            if positions[ticker]['quantity'] <= 0 < positions[ticker]['buy_price'] or len(
                    positions[ticker]['sell_prices']) == 3:
                last_sell_price = positions[ticker]['sell_prices'][-1]
                while len(positions[ticker]['sell_prices']) < 3:
                    positions[ticker]['sell_prices'].append(last_sell_price)

                _quantity = positions[ticker]['init_quantity']

                _commission = positions[ticker]['commission']
                _commission_str = "${:.2f}".format(_commission)

                s1, s2, s3 = positions[ticker]['sell_prices']
                s1_str = "${:.2f}".format(s1)
                s2_str = "${:.2f}".format(s2)
                s3_str = "${:.2f}".format(s3)

                buy_price = positions[ticker]['buy_price']
                buy_price_str = "${:.2f}".format(buy_price)

                profit_percentage = ((s1 - buy_price) * 0.5 + (s2 - buy_price) * 0.3 + (
                            s3 - buy_price) * 0.2) / buy_price
                profit_percentage_str = "{:.6f}%".format(profit_percentage * 100)

                pnl = profit_percentage * (buy_price * _quantity) - _commission
                pnl_str = "${:.2f}".format(pnl)

                init_total_price = buy_price * _quantity
                init_total_price_str = "${:.2f}".format(init_total_price)

                processed_data = [
                    trade_time,  # 最后一次交易时间
                    ticker,  # symbol
                    buy_price_str,  # 买入价
                    s1_str, s2_str, s3_str,  # 卖出价
                    _quantity,  # 最初买入数量
                    init_total_price_str,  # 最初买入仓位
                    profit_percentage_str,  # pnl rate
                    pnl_str,  # pnl
                    _commission_str  # 手续费
                ]
                async with lock_visualize_record:
                    async with aiofiles.open('可视化记录.csv', 'a', newline='', encoding='utf-8') as csvfile:
                        await csvfile.write(','.join(map(str, processed_data)) + '\n')
                del positions[ticker]

        await save_positions(positions)
    except Exception as e:
        logging.warning("Visualized_data记录失败,错误：%s，订单详情：", e, record)


def send_email(ticker, action, quantity, initial_price):
    gmail_user = mail

    msg = MIMEText('Symbol：%s, \r\n方向：%s, \r\n数量: %s, \r\n初始价格: %s -> 当前价格: %s' % (
        ticker, action, quantity, initial_price, SYMBOLS[ticker][0]))
    msg['Subject'] = ('警告: %s 订单卖出失败，请立即检查订单状态！' % ticker)
    msg['From'] = gmail_user
    msg['To'] = 'joe.trading1016@gmail.com'  # 收件人邮箱

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.ehlo()
        server.login(gmail_user, mail_password)
        server.sendmail(gmail_user, 'joe.trading1016@gmail.com', msg.as_string())
        server.close()
        logging.warning('邮件发送成功！时间: %s', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
    except Exception as e:
        logging.warning('邮件发送失败: %s, 时间: %s', e, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))


if __name__ == "__main__":
    logging.basicConfig(filename='app.log', level=logging.INFO)
    thread = Thread(target=run_asyncio_loop)
    thread.start()

    push_client.connect_callback = connect_callback
    start_listening()
    app.run('0.0.0.0', 80)
