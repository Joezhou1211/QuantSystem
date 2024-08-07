from logging.handlers import RotatingFileHandler
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
import signal
import sys
from tigeropen.common.util.signature_utils import read_private_key


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
ORDER_ID_var = 0

cash_lock = Lock()
lock_raw_data = asyncio.Lock()
lock_order_record = asyncio.Lock()
lock_filled_order_record = asyncio.Lock()
lock_positions_json = asyncio.Lock()
lock_visualize_record = asyncio.Lock()
lock_priceAndVolume = asyncio.Lock()

mail = 'mail'
mail_password = 'password'
order_dict = {}
Trading_Percentage = 0.3325  # 在这里修改交易比例

"""
需要的更新：
    1. 将LMT更新为永久GTC   -> 已完成
    2. 将现有订单升级为Queue base的排序系统  -> 已完成

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
    summer_time = is_daylight_saving()

    # 根据夏令时或冬令时决定运行哪个版本的 set_market_status 函数
    if summer_time:
        loop.run_until_complete(
            asyncio.gather(
                set_market_status_summer_time(),
                handle_price_signals(),
                handle_trade_signals()
                # , get_market_status()
            )
        )
    else:
        loop.run_until_complete(
            asyncio.gather(
                set_market_status_winter_time(),
                handle_price_signals(),
                handle_trade_signals()
                # , get_market_status()
            )
        )


def is_daylight_saving():  # 检查夏令时
    tz = pytz.timezone('America/New_York')
    now = datetime.datetime.now(tz)
    return now.dst() != datetime.timedelta(0)



async def set_market_status_winter_time():
    global SET_STATUS, STATUS
    while True:
        try:
            current_time = dt.now()
            current_weekday = current_time.weekday()
            current_only_time = current_time.time()

            sec = 59  # 在这里修改秒
            mid_night = t(23, 59, sec)  # new
            pre_open = t(18, 59, sec)  # new
            trading_open = t(00, 29, sec)  # new
            post_open = t(6, 59, sec)  # new
            day_close = t(10, 59, 59)  # 固定
            not_yet_open = t(14, 59, 59)  # 固定

            if current_weekday == 0:  # 周一
                if current_only_time <= not_yet_open:  # 0:00 - 15:00
                    SET_STATUS = "MARKET_CLOSED"  # done
                elif not_yet_open < current_only_time <= pre_open:  # 15:00 - 19:00
                    SET_STATUS = "NOT_YET_OPEN"
                else:  # 19:00 - 23:59:59
                    SET_STATUS = "PRE_HOUR_TRADING"

            elif 1 <= current_weekday <= 4:  # 周二到周五
                if current_only_time <= trading_open:
                    SET_STATUS = "PRE_HOUR_TRADING"
                elif trading_open < current_only_time <= post_open:
                    SET_STATUS = "TRADING"
                elif post_open < current_only_time <= day_close:
                    SET_STATUS = "POST_HOUR_TRADING"
                elif day_close < current_only_time <= not_yet_open:
                    SET_STATUS = "CLOSING"
                elif pre_open <= current_only_time <= mid_night:
                    SET_STATUS = "PRE_HOUR_TRADING"
                elif not_yet_open <= current_only_time <= pre_open:
                    SET_STATUS = "NOT_YET_OPEN"

            elif current_weekday == 5:  # 周六
                if current_only_time <= trading_open:
                    SET_STATUS = "PRE_HOUR_TRADING"
                elif trading_open < current_only_time <= post_open:  # 23:30 - 5:59
                    SET_STATUS = "TRADING"
                elif post_open < current_only_time <= day_close:
                    SET_STATUS = "POST_HOUR_TRADING"
                elif day_close < current_only_time <= not_yet_open:
                    SET_STATUS = "CLOSING"
                else:
                    SET_STATUS = "MARKET_CLOSED"

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


async def set_market_status_summer_time():  # 按照预设时间更新市场状态
    global SET_STATUS, STATUS
    while True:
        try:
            current_time = dt.now()
            current_weekday = current_time.weekday()
            current_only_time = current_time.time()
            sec = 59  # 在这里修改秒
            pre_open = t(17, 59, sec)
            trading_open = t(23, 29, sec)
            post_open = t(5, 59, sec)
            day_close = t(9, 59, 59)  # 固定
            not_yet_open = t(14, 00, 00)

            if current_weekday == 0:  # 周一
                if current_only_time < not_yet_open:  # 0:00 - 14:00
                    SET_STATUS = "MARKET_CLOSED"
                elif not_yet_open <= current_only_time <= pre_open:  # 14:00 - 17:59
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
            logging.warning("市场状态出现错误，已校准。市场状态：%s -> 新市场状态：%s", STATUS, check_status, )
            STATUS = check_status
        await asyncio.sleep(300)


price_queue = asyncio.Queue()  # 用于价格信号
trade_queue = asyncio.Queue()  # 用于交易信号

orderid_lock = Lock()
order_ids = 1


def generate_orderid():
    global order_ids
    with orderid_lock:
        current_orderid = order_ids
        order_ids += 1
    return current_orderid


async def handle_price_signals():
    while True:
        _data = await price_queue.get()
        SYMBOLS[_data['symbol']] = [round(float(_data['price']), 2), float(_data['volume'])]
        await asyncio.sleep(0.5)


async def handle_trade_signals():
    global ORDER_ID_var
    while True:
        _data = await trade_queue.get()
        print("")
        print("============= START ==============")
        print("---------- 收到交易信号 ----------")
        if 'action' in _data and 'symbol' in _data and 'price' in _data:
            current_orderid = generate_orderid()
            ORDER_ID_var = current_orderid
            await record_to_csvTEST2(
                [current_orderid, _data['symbol'], _data['action'], _data['price'], _data['percentage'],
                 time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())])
            asyncio.create_task(
                place_order(_data['action'], _data['symbol'], _data['price'], current_orderid, _data['percentage']))
        await asyncio.sleep(1)


@app.route("/identityCheck", methods=['POST'])
async def identityCheck():
    _data = json.loads(request.data)
    try:
        if 'apiKey' in _data and _data.get('apiKey') == "7a8b2c1d-9e0f-4g5h-6i7j-8k9l0m1n2o3p":
            await price_queue.put(_data)
        if 'apiKey' in _data and _data.get('apiKey') == "3f90166a-4cba-4533-86f2-31e690cfabb9":
            await trade_queue.put(_data)
        return {"status": "信号已添加到队列"}
    except Exception as e:
        logging.warning(f"将信号添加到队列时出现错误: {str(e)}")
        return {"status": "error", "message": str(e)}


async def priceAndVolume(symbol, price, volume):
    async with lock_priceAndVolume:
        SYMBOLS[symbol] = [round(float(price), 2), float(volume)]


# ------------------------------------------------------------Tiger Client Function--------------------------------------------------------------------------------------------#


def get_client_config():
    client_configs = TigerOpenClientConfig()
    client_configs.private_key = read_private_key('/home/admin/mykey.pem')
    client_configs.tiger_id = '0123456'
    client_configs.account = '0123456'  
    client_configs.language = Language.zh_CN
    return client_configs


client_config = get_client_config()
protocol, host, port = client_config.socket_host_port
push_client = PushClient(host, port, use_ssl=(protocol == 'ssl'), use_protobuf=True)
trade_client_outer = TradeClient(client_config)
portfolio_account_outer = trade_client_outer.get_prime_assets(base_currency='USD')
LIQUIDATION = portfolio_account_outer.segments['S'].net_liquidation


def connect_callback(frame):  # 回调接口 初始化当前Cash/总资产/持仓/json
    global CASH, NET_LIQUIDATION, POSITION
    trade_client = TradeClient(client_config)
    portfolio_account = trade_client.get_prime_assets(base_currency='USD')
    CASH = portfolio_account.segments['S'].cash_available_for_trade
    NET_LIQUIDATION = portfolio_account.segments['S'].net_liquidation
    position = trade_client.get_positions(account=client_config.account, sec_type=SecurityType.STK, currency='USD',
                                          market=Market.US)
    local_positions = load_position()  # 读取本地持仓
    api_symbols = set(pos.contract.symbol for pos in position)  # API 持仓中的标的

    if len(position) > 0:
        for pos in position:
            POSITION[pos.contract.symbol] = [pos.quantity, 0]
            symbol = pos.contract.symbol
            if symbol in local_positions:
                # 检查数量是否相同，如果不相同则更新
                if local_positions[symbol]['quantity'] != pos.quantity:
                    local_positions[symbol]['quantity'] = pos.quantity
                    local_positions[symbol]['init_quantity'] = pos.quantity
            else:
                # 如果不在 JSON 中，则添加新条目
                local_positions[symbol] = {
                    "buy_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "buy_price": pos.average_cost,
                    "sell_prices": [],
                    "quantity": pos.quantity,
                    "init_quantity": pos.quantity,
                    "commission": 0
                }

        # 从 JSON 中删除不存在于 API 持仓中的持仓
        for symbol in list(local_positions.keys()):
            if symbol not in api_symbols:
                del local_positions[symbol]

        # 保存更新后的 JSON
        save_position(local_positions)

    if frame:
        print("============================================================================")
        print('回调系统连接成功, 当前时间:', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), '市场状态：', STATUS)
        cash_percentage = str(100 * round(CASH / NET_LIQUIDATION, 2)) + '%'
        print("百分比预设 ", str(math.ceil(Trading_Percentage * 100)) + "%/Trade")
        print("可用现金: $USD", CASH, "| 百分比：", cash_percentage)
        print('总资产: $USD ', NET_LIQUIDATION)
        if not POSITION:
            print('当前持仓: 无')
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
    status = str(status_enum).split('.')[1]
    logging.info("|%s|状态: %s", ORDER_ID_var, status)


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

    logging.info("|%s|检查重复订单中", orderid)
    open_orders = trade_client.get_open_orders(symbol=symbol)
    if not open_orders:
        return True, None, None
    order = open_orders[0]
    is_trading_hour = STATUS == "TRADING"

    holding = POSITION[symbol][0] if symbol in POSITION else 0  # 获取标的现有持仓
    sellingQuantity = int(math.ceil(holding * percentage))  # 计算新订单卖出数量
    if sellingQuantity > POSITION[symbol][0] if symbol in POSITION else 0:
        sellingQuantity = POSITION[symbol][0] if symbol in POSITION else 0

    compare_price = order.latest_price if is_trading_hour else order.limit_price
    old_order_price = compare_price  # 以市价单下单时的价格作为输出
    if not is_trading_hour:
        old_order_price = order.limit_price
    old_orderid = order_dict[order.id][0] if order.id in order_dict else None
    orderid_str = str(old_orderid).center(4)

    if order.action == 'BUY':
        if new_action == 'SELL':
            if compare_price > new_price:  # 1 取消两个订单
                trade_client.cancel_order(id=order.id)
                logging.warning(
                    "●|%s|<->|%s|取消 %s 旧%s, %s, %s, 新%s, %s, %s, ref(1)",
                    orderid_str, orderid, symbol, order.action, order.quantity, old_order_price, new_action,
                    sellingQuantity,
                    new_price)

                if order.id not in order_dict:
                    order_dict[order.id] = [0]
                order_dict[order.id].append('与新订单方向冲突被取消')
                return False, order, 'CANCEL'

            elif compare_price <= new_price:
                if percentage < 1:  # 2 仅改变数量
                    quantity = int(abs(percentage - 1) * order.quantity)
                    logging.warning(
                        "●|%s|->>|%s|合并 %s 旧%s, %s, %s, 新%s, %s, %s, 合并为->%s, %s, %s. ref(2)",
                        orderid_str, orderid, symbol,
                        order.action, order.quantity, old_order_price,
                        new_action, sellingQuantity, new_price,
                        order.action, quantity, old_order_price)
                    if is_trading_hour:
                        trade_client.modify_order(order=order, quantity=quantity)
                    if not is_trading_hour:
                        trade_client.modify_order(order=order, quantity=quantity, limit_price=order.limit_price)
                    return False, order, 'MODIFY'

                if percentage == 1:  # 3 取消两个订单
                    trade_client.cancel_order(id=order.id)
                    logging.warning(
                        "●|%s|<->|%s|取消 %s 旧%s, %s, %s, 新%s, %s, %s, ref(3)",
                        orderid_str, orderid, symbol, order.action, order.quantity, old_order_price, new_action,
                        sellingQuantity,
                        new_price)
                    if order.id not in order_dict:
                        order_dict[order.id] = [0]
                    order_dict[order.id].append('与新订单方向冲突被取消')
                    return False, order, 'CANCEL'

        elif new_action == 'BUY':
            return True, None, None  # 只有 buy->buy 返回None

    elif order.action == 'SELL' and new_action in {'BUY', 'SELL'}:
        if new_action == 'BUY':  # 4 仅取消旧订单
            trade_client.cancel_order(id=order.id)
            quantity = int((NET_LIQUIDATION * 0.25) // new_price)
            logging.warning(
                "●|%s|<<-取消旧订单 %s 旧%s, %s, %s, 新%s, %s, %s, ref(4)",
                orderid_str, symbol, order.action, order.quantity, old_order_price, new_action, quantity,
                new_price)
            if order.id not in order_dict:
                order_dict[order.id] = [0]
            order_dict[order.id].append('旧订单被新订单取代，旧订单被取消')
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
                "●|%s|->>|%s|合并 %s 旧%s, %s, %s, 新%s, %s, %s, 合并为->%s, %s, %s. ref(5)",
                orderid_str, orderid, symbol,
                order.action, order.quantity, old_order_price,
                new_action, sellingQuantity, new_price,
                order.action, quantity, new_price)
            return False, order, 'MODIFY'


async def place_order(action, symbol, price, orderid, percentage=1.00):  # 盘中
    logging.info("=========================|%s|订单生成中=========================", orderid)
    global POSITION
    unfilledPrice = 0
    trade_client = TradeClient(client_config)
    price = round(float(price), 2)
    action = action.upper()
    percentage = float(percentage)
    order = None
    holds = '否'

    logging.info("|%s|订单基础信息: %s, %s, %s, %s", orderid, symbol, action, price, percentage)
    if symbol in list(POSITION.keys()):
        holds = '是'
    cash_percentage = str(round(100 * CASH / NET_LIQUIDATION, 2)) + '%'
    logging.info("|%s|当前可用金额百分比: %s, 是否持有当前标的: %s", orderid, cash_percentage, holds)

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
            logging.info("|%s|旧订单已取消，%s %s,订单最后更新时间: %s, 下单时间: %s, 订单号:%s", orderid,
                         old_order.action,
                         old_order.quantity, datetime.datetime.fromtimestamp(old_order.update_time / 1000),
                         datetime.datetime.fromtimestamp(old_order.order_time / 1000), old_order.id)
        if identifier == 'MODIFY':
            if STATUS == "TRADING":
                await order_filled(old_order, unfilledPrice, orderid)
            else:
                await postHourTradesHandling(trade_client, old_order, old_order.limit_price, orderid)
            return

    if not checker:
        return

    max_buy = NET_LIQUIDATION * Trading_Percentage
    max_quantity = int(max_buy // price)
    contract = stock_contract(symbol=symbol, currency='USD')

    if STATUS == "TRADING":
        if action == "BUY" and CASH >= max_buy:
            order = market_order(account=client_config.account, contract=contract, action=action,
                                 quantity=max_quantity)

        if action == "BUY" and CASH < max_buy:
            logging.info("|%s|买入 %s 失败，现金不足", orderid, symbol)
            logging.info("=========================|%s|订单已结束=========================\r\n", orderid)
            print("[盘中]买入", symbol, " 失败，现金不足")
            return

        if action == "SELL":
            quantity = POSITION[symbol][0] if symbol in POSITION else 0
            position = trade_client.get_positions(account=client_config.account, sec_type=SecurityType.STK,
                                                  currency='USD',
                                                  market=Market.US, symbol=symbol)
            if quantity > 0 and position:
                POSITION[symbol][1] = quantity  # 本次下单时的持仓数量
                sellingQuantity = int(math.ceil(quantity * percentage))
                if sellingQuantity > POSITION[symbol][0] if symbol in POSITION else 0:
                    sellingQuantity = POSITION[symbol][0] if symbol in POSITION else 0
                order = market_order(account=client_config.account, contract=contract, action=action, quantity=sellingQuantity)

            else:
                print("[盘中] 交易失败，当前没有", symbol, "的持仓")
                logging.info("|%s|卖出失败，当前没有 %s 的持仓", orderid, symbol)
                logging.info("=========================|%s|订单已结束=========================\r\n", orderid)
                print("============== END ===============")
                return

        if order:
            order_id_main = trade_client.place_order(order)
            print("----------------------------------")
            print("[盘中]标的", symbol, "|", order.action, " 下单成功。\n\rPrice: $", price, "订单:", orderid)
            print("----------------------------------")
            orders = trade_client.get_order(id=order_id_main)
            order_status[order_id_main] = orders.status
            order_dict[orders.id] = [orderid]

            await record_to_csvTEST(
                [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), orders.contract.symbol, orders.action, price,
                 orders.quantity, orders.id, orderid])  # test

            sleep_time = 10
            if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
                sleep_time = 1
            await asyncio.sleep(sleep_time)
            await order_filled(orders, unfilledPrice, orderid)
        else:
            logging.warning("订单为空, 标的: %s, Action: %s, Price: %s, Percentage: %s", symbol, action, price,
                            percentage)
            return

    if STATUS == "POST_HOUR_TRADING" or STATUS == "PRE_HOUR_TRADING":
        if action == "BUY" and CASH >= max_buy:
            order = limit_order(account=client_config.account, contract=contract, action=action,
                                quantity=max_quantity,
                                limit_price=round(price, 2))
            order.time_in_force = 'GTC'

        if action == "BUY" and CASH < max_buy:
            logging.info("|%s|买入 %s 失败，现金不足", orderid, symbol)
            logging.info("=========================|%s|订单已结束=========================\r\n", orderid)
            print("[盘后]买入", symbol, " 失败，现金不足")
            return

        if action == "SELL":
            quantity = POSITION[symbol][0] if symbol in POSITION else 0
            position = trade_client.get_positions(account=client_config.account, sec_type=SecurityType.STK,
                                                  currency='USD',
                                                  market=Market.US, symbol=symbol)
            if quantity > 0 and position:
                POSITION[symbol][1] = quantity  # 本次下单时的持仓数量
                sellingQuantity = int(math.ceil(quantity * percentage))
                if sellingQuantity > POSITION[symbol][0] if symbol in POSITION else 0:
                    sellingQuantity = POSITION[symbol][0] if symbol in POSITION else 0
                order = limit_order(account=client_config.account, contract=contract, action=action,
                                    quantity=sellingQuantity,
                                    limit_price=round(price * 0.99995, 2))
                order.time_in_force = 'GTC'

            else:
                print("[盘后] 交易失败，当前没有", symbol, "的持仓")
                logging.info("|%s|卖出失败，当前没有 %s 的持仓", orderid, symbol)
                logging.info("=========================|%s|订单已结束=========================\r\n", orderid)
                print("============== END ===============")
                return

        if order:
            order_id_main = trade_client.place_order(order)
            print("[盘后]标的", symbol, "|", order.action, " 第 1 次下单, 成功。\n\rPrice: $", price, "订单号:", orderid)

            orders = trade_client.get_order(id=order_id_main)
            order_status[order_id_main] = orders.status  # 初始化订单状态
            order_dict[orders.id] = [orderid]  # 初始化订单号与本地订单号键对

            await record_to_csvTEST(
                [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), orders.contract.symbol, orders.action, price,
                 orders.quantity, orders.id, orderid])
            sleep_time = 10
            if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
                sleep_time = 1
            await asyncio.sleep(sleep_time)
            await postHourTradesHandling(trade_client, orders, unfilledPrice, orderid)
        else:
            logging.warning("订单为空, 标的: %s, Action: %s, Price: %s, Percentage: %s", symbol, action, price,
                            percentage)
            return


async def check_position(orders):
    """
    # 实时仓位仓位应该直接去POSITION里面找 更改的仓位才应该被返回来
    # 情况1 -> 无持仓 -> 判断是否为卖 卖则取消
    # 情况2 -> 仓位改变 -> 判断改变的数量 返回作为改单的新数量 限定不能超过最大值
    # 情况3 -> 仓位无变化 -> 返回原来订单数量orders.quantity
    """
    # logging.info("|%s|check point 0", orderid)
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


async def postHourTradesHandling(trade_client, orders, unfilledPrice, orderid):
    global POSITION, SYMBOLS
    logging.info("|%s|进行盘后交易循环", orderid)
    trade_attempts = 2
    initial_price = orders.limit_price
    checker = 0
    orderid_str = str(orderid).center(4)
    first_time_check = True

    while True:
        quantity = await check_position(orders)
        # logging.info("|%s|check point 1", orderid)
        if not quantity:
            return

        if STATUS == "POST_HOUR_TRADING" or STATUS == "PRE_HOUR_TRADING":
            # logging.info("|%s|check point 2", orderid)
            if not orders.remaining and (
                    order_status.get(orders.id, None) == OrderStatus.FILLED or orders.status == OrderStatus.FILLED):
                await order_filled(orders, unfilledPrice, orderid)
                return
            elif ((order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                         OrderStatus.REJECTED])
                  and orders.remaining == orders.quantity and not orders.filled > 0 and
                  (orders.reason not in ['改单成功', '', None, str(orders.contract.symbol)])) or \
                    order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                          OrderStatus.REJECTED]:
                logging.warning(
                    "|%s|订单取消:%s %s",
                    str(orderid).center(5), orders.reason,
                    order_dict[orders.id][1] if orders.id in order_dict and len(order_dict[orders.id]) > 1 else None)
                return
            else:
                # logging.info("|%s|check point 3", orderid)
                symbol = orders.contract.symbol
                if symbol in SYMBOLS:  # 检测标的
                    volume = SYMBOLS[symbol][1]
                    price = SYMBOLS[symbol][0]
                    oldPrice = orders.limit_price
                    # logging.info("|%s|check point 4", orderid)
                    if abs(price - oldPrice) <= 0.029:  # 价格变动3分钱以下不改单
                        # logging.info("|%s|check point 4.1", orderid)
                        orders = trade_client.get_order(id=orders.id)
                        # logging.info("|%s|check point 4.2", orderid)
                        if orders.status == OrderStatus.FILLED:
                            # logging.info("|%s|check point 4.3", orderid)
                            await order_filled(orders, unfilledPrice, orderid)
                            return
                        await asyncio.sleep(10)  # 如果价格没变化 继续等 如果变化了才重新下单
                        # logging.info("|%s|check point 4.4", orderid)
                        continue

                    else:
                        if (oldPrice - price) / oldPrice >= 0.2 and volume >= 1000:
                            price = round(price * 0.992, 2)  # 极端情况改单
                            send_email(orders.contract.symbol, orders.action, orders.quantity, initial_price)
                        orders = trade_client.get_order(id=orders.id)
                        # logging.info("|%s|check point 4.5", orderid)
                        if orders.status == OrderStatus.FILLED:
                            # logging.info("|%s|check point 4.6", orderid)
                            await order_filled(orders, unfilledPrice, orderid)
                            return
                        trade_client.modify_order(orders, limit_price=price, quantity=quantity)
                        # logging.info("|%s|check point 4.7", orderid)
                        logging.warning("●|%s|%s|%s|第%s次下单 $%s -> $%s",
                                        orderid_str, orders.contract.symbol.ljust(5), orders.action.ljust(4),
                                        trade_attempts,
                                        oldPrice, price)

                        unfilledPrice = price
                        # logging.info("|%s|check point 4.8", orderid)
                        trade_attempts += 1
                        # logging.info("|%s|check point 5, 休眠20s", orderid)
                        await asyncio.sleep(20)
                        orders = trade_client.get_order(id=orders.id)
                        continue
                else:
                    # logging.info("|%s|check point 5.1", orderid)
                    if trade_attempts == 2 and not checker:
                        # logging.info("|%s|check point 5.2", orderid)
                        checker = 1
                        # logging.info("|%s|自动改单失败，当前标价格还未更新", orderid)
                        await asyncio.sleep(30)
                        # logging.info("|%s|check point 5.3", orderid)
                        continue
                    # symbol_list = list(SYMBOLS.keys())
                    # logging.info("|%s|自动改单报错，SYMBOLS：%s 不存在当前标的: %s,", orderid, symbol_list, symbol)
                    await asyncio.sleep(30)
                    # logging.info("|%s|check point 5.4", orderid)
                    continue

        if STATUS == "TRADING":  # 盘前 没改成， 开盘了
            contract = orders.contract
            action = orders.action
            quantity = orders.remaining
            trade_client.cancel_order(id=orders.id)  # 取消原有的限价单
            await postToTrading(contract, action, quantity, trade_client, unfilledPrice, orderid)
            break
        if STATUS in ["CLOSING", "NOT_YET_OPEN", "MARKET_CLOSED", "EARLY_CLOSED"]:
            if not first_time_check:
                await asyncio.sleep(10)
            if first_time_check:
                logging.warning("[交易时间超出当日交易时段]已经挂起订单|%s|等待盘前后继续交易,标的: %s|方向: %s|",
                                orderid,
                                orders.contract.symbol,
                                orders.action)
                await asyncio.sleep(28700)
                first_time_check = False
            continue


async def order_filled(orders, unfilledPrice, orderid):
    """
    创建逻辑流 所有订单都永久挂单 直到成交 -> GTC 实盘完成
    """

    logging.info("|%s|交易进入结束环节", orderid)
    priceDiff = None
    priceDiffPercentage = None
    trade_client = TradeClient(client_config)
    i = 1
    orderid_str = str(orderid).ljust(4)
    while True:
        if i >= 10:
            orders = trade_client.get_order(id=orders.id)

            if order_status.get(orders.id, None) != orders.status:
                if not order_status.get(orders.id, None):  # 如果识别到订单状态为空 则直接返回
                    return
                logging.warning("●|%s|状态校准成功: %s -> %s", orderid_str, order_status.get(orders.id, None),
                                orders.status)
                order_status[orders.id] = orders.status
            i = 1
            continue
        if not orders.remaining and order_status.get(orders.id, None) == OrderStatus.FILLED:
            if unfilledPrice:
                priceDiff = round(abs(orders.avg_fill_price - unfilledPrice), 4)
                if priceDiff > 0.01:
                    priceDiffPercentage = round(priceDiff / unfilledPrice * 100, 4)
                    logging.warning("●|%s|滑点:$%s 百分比:%s%%", orderid_str, priceDiff,
                                    priceDiffPercentage)
                else:
                    priceDiff = ''
                    priceDiffPercentage = ''

            if not priceDiff:
                priceDiff = ''
                priceDiffPercentage = ''

            orderid_str = str(orderid).center(5)

            logging.warning(
                "|%s|%s|%s|%s|均价%s|佣金%s|成交额%s|",
                orderid_str,
                orders.contract.symbol.ljust(5),
                orders.action.ljust(4),
                str(orders.quantity).ljust(4),
                "${:6.2f}".format(orders.avg_fill_price),
                "${:5.2f}".format(orders.commission),
                "${:9.2f}".format(orders.filled * orders.avg_fill_price)
            )

            data = [orders.contract.symbol, orders.action, orders.quantity, orders.avg_fill_price,
                    orders.commission,
                    round(orders.filled * orders.avg_fill_price, 2), STATUS,
                    datetime.datetime.fromtimestamp(orders.trade_time / 1000), orders.id, priceDiff,
                    priceDiffPercentage]

            await csv_visualize_data(data)
            await record_to_csv(data + [orderid])

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
            if orders.contract.symbol in POSITION:
                if orders.quantity == POSITION[orders.contract.symbol][0] == POSITION[orders.contract.symbol][1] and \
                        orders.action == 'SELL':
                    del POSITION[orders.contract.symbol]
                logging.info("=========================|%s|订单已结束=========================\r\n", orderid)
                return
            return

        elif ((order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                     OrderStatus.REJECTED]) and (
                      orders.reason not in ['改单成功', '', 'Change order succeeded', None,
                                            str(orders.contract.symbol)])):
            logging.warning("======|%s|出错%s", orderid, orders)
            if orders.id in order_status:
                del order_status[orders.id]
            return
        else:
            await asyncio.sleep(3)
            i += 1


async def postToTrading(contract, action, quantity, trade_client, unfilledPrice, orderid):
    logging.info("|%s|从post进入trading，订单类型改变中", orderid)
    order = market_order(account=client_config.account, contract=contract, action=action, quantity=quantity)
    orders = trade_client.place_order(order)
    order_status[orders.id] = orders.status
    logging.warning("●|%s|盘前->开盘 重新下单成功", str(orderid).center(4))
    await asyncio.sleep(10)
    while True:
        if order_status.get(orders.id, None) == OrderStatus.FILLED:
            await order_filled(orders, unfilledPrice, orderid)
        elif ((order_status.get(orders.id, None) in [OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                                     OrderStatus.REJECTED]) and (
                      orders.reason not in ['改单成功', '', 'Change order succeeded', None,
                                            str(orders.contract.symbol)])):
            logging.warning("|%s|订单出现问题", orderid)
            return
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


def load_position():
    try:
        with open('持仓.json', 'r', encoding='utf-8') as f:
            data = f.read()
            return json.loads(data)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_position(positions):
    with open('持仓.json', 'w', encoding='utf-8') as f:
        f.write(json.dumps(positions))


total_pnl = 0
total_pnl_rate = 0
total_commission = 0
started_fund = LIQUIDATION
win = 0
lose = 0
win_rate = 0
final_data = {}


async def csv_visualize_data(record):
    global total_pnl, total_pnl_rate, win, lose, win_rate, final_data, total_commission
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

        if positions[ticker]['quantity'] < 0:
            del positions[ticker]
            return

        elif positions[ticker]['quantity'] == 0 or len(positions[ticker]['sell_prices']) == 3:
            last_sell_price = positions[ticker]['sell_prices'][-1]
            while len(positions[ticker]['sell_prices']) < 3:
                positions[ticker]['sell_prices'].append(last_sell_price)

            _quantity = positions[ticker]['init_quantity']

            _commission = positions[ticker]['commission']
            total_commission += _commission
            _commission_str = "${:.2f}".format(_commission)
            total_commission_str = "${:.2f}".format(total_commission)

            s1, s2, s3 = positions[ticker]['sell_prices']
            s1_str = "${:.2f}".format(s1)
            s2_str = "${:.2f}".format(s2)
            s3_str = "${:.2f}".format(s3)

            buy_price = positions[ticker]['buy_price']
            buy_price_str = "${:.2f}".format(buy_price)

            if not buy_price:
                return
            profit_percentage = ((s1 - buy_price) * 0.5 + (s2 - buy_price) * 0.3 + (
                    s3 - buy_price) * 0.2) / buy_price
            profit_percentage_str = "{:.6f}%".format(profit_percentage * 100)
            pnl = profit_percentage * (buy_price * _quantity) - _commission
            total_pnl += pnl
            total_pnl_rate = total_pnl / started_fund
            pnl_str = "${:.2f}".format(pnl)
            total_pnl_str = "${:.2f}".format(total_pnl)
            total_pnl_rate_str = "{:.2f}%".format(total_pnl_rate * 100)

            init_total_price = buy_price * _quantity
            init_total_price_str = "${:.2f}".format(init_total_price)

            profit_percentage_with_commission = pnl / init_total_price
            profit_percentage_with_commission_str = "{:.2f}%".format(profit_percentage_with_commission * 100)

            pnl_with_commission_str = "${:.2f}".format(profit_percentage * (buy_price * _quantity))

            processed_data = [
                trade_time,  # 最后一次交易时间
                ticker,  # symbol
                buy_price_str,  # 买入价
                s1_str, s2_str, s3_str,  # 卖出价
                _quantity,  # 最初买入数量
                init_total_price_str,  # 买入总价
                profit_percentage_str,  # pnl rate无手续费
                profit_percentage_with_commission_str,  # pnl rate有手续费
                pnl_str,  # pnl无手续费
                pnl_with_commission_str,  # pnl有手续费
                _commission_str,  # 手续费
            ]

            if pnl > 0:
                win += 1
                win_str = str(win)
                final_data['win'] = win_str
            if pnl < 0:
                lose += 1
                lose_str = str(lose)
                final_data['lose'] = lose_str

            if win + lose > 0:
                win_rate = win / (win + lose)
                win_rate_str = "{:.2f}%".format(win_rate * 100)
                final_data['win_rate'] = win_rate_str

            # 更新 final_data 字典
            final_data['total_pnl'] = total_pnl_str
            final_data['total_pnl_rate'] = total_pnl_rate_str
            final_data['total_commission'] = total_commission_str

            async with lock_visualize_record:
                async with aiofiles.open('可视化记录.csv', 'a', newline='', encoding='utf-8') as csvfile:
                    await csvfile.write(','.join(map(str, processed_data)) + '\n')
            del positions[ticker]

    await save_positions(positions)


is_finished = False


def finish_up():
    global is_finished
    if not is_finished:
        print('\n\r执行结束操作......')
        with open('可视化记录.csv', 'a', newline='', encoding='utf-8') as csvfile:
            csvfile.write(','.join(final_data.keys()) + '\n')
            values = ','.join(map(str, final_data.values())) + '\n'
            csvfile.write(values)
            print('文件写入完成!')
        is_finished = True
        return
    elif is_finished:
        print('\n\r正在关闭程序......')


def signal_handler(sig, frame):
    finish_up()
    sys.exit(0)


# 设置信号处理器
signal.signal(signal.SIGINT, signal_handler)


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
        logging.warning("邮件发送成功")
    except Exception as e:
        logging.warning('邮件发送失败: %s', e)


if __name__ == "__main__":
    time_T = str(time.strftime('%y-%m-%d|%H:%M', time.localtime())) + '.log'
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    fh = RotatingFileHandler(time_T, maxBytes=1 * 1024 * 1024, backupCount=7)  # 最大1MB，备份7个
    formatter = logging.Formatter('%(asctime)s-%(message)s', datefmt='%D %H:%M:%S')
    fh.setFormatter(formatter)

    logger.addHandler(fh)
    print("系统启动中...")
    print("----------------------------------------------------------------------------")

    thread = Thread(target=run_asyncio_loop)
    thread.start()

    push_client.connect_callback = connect_callback
    start_listening()
    app.run('0.0.0.0', 8080)
