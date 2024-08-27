# English
# Quantitative System

This is a quantitative trading system implemented in Python. The system is based on the TigerOpen API and executes buy and sell orders as well as updates local data by receiving trading signals from TradingView. To run this system, it needs to be deployed on a server.

## Notes

- The author has not purchased Tiger's real-time data, so the system cannot detect pre-market and post-market statuses. However, a workaround method has been implemented to determine trading status based on server time, with checks every 5 minutes. This might need recalibration depending on daylight saving time adjustments.
- The same issue applies: without real-time data, the method to obtain real-time prices for order modification is to use TradingView to send price signals every minute. TradingView sends the monitored price changes via webhook every minute. For basic paid users, up to 20 trading signals and 20 price signals can be created simultaneously. This means the system can automatically trade up to 20 stocks at once.
- This project was written by the author during their freshman year as a learning project. Although the system has undergone 6 months of stress testing and it's 99.9% certain that intraday trading will not encounter issues, if it's intended for production use, ensure to check all details and understand the definition of every function and variable.
- Currently, the author is developing a semi-automated Python graphical trading system for intraday trading. Features include fractal identification, preset pattern stock selection, automatic price tracking, real-time news monitoring, and large model-based decision-making suggestions.

## System Features

- **Low to Medium Frequency Trading:** Supports minute-level trading with automated order placement, modification, and cancellation. Issues may arise when the frequency exceeds one trade per minute. Python does not support concurrency; for high-frequency trading, other coding options should be considered.
- **Optimal Deployment Location:** It is recommended to deploy the server near Washington, D.C., to optimize latency for U.S. stock trading.
- **Flexible Integration:** If you wish to bypass TradingView, consider using vector bt or other open-source frameworks for strategy and backtesting, but ensure real-time data issues are properly addressed.
- **Automation:** The system supports automated order placement, modification, cancellation, monitoring, and notifications. Current features include after-hours trading, and order modifications based on volume. If after-hours trading is not needed, you can disable this function by modifying the `place_order` function.

## Functionality

- **Market State Management:** Automatically updates market status, accounting for daylight saving and standard time.
- **Order Management:** Handles concurrent data, manages order queues, ensuring correct order execution and modification.
- **Real-Time Data Processing:** Integrates TigerOpen API for real-time market data and order status updates.
- **Logging and Email Notifications:** Detailed logging and email notifications for critical events.
- **CSV Recording:** Logs all trades and positions into a CSV file for analysis and visualization.

## Prerequisites
```
Python 3.7+
Required Libraries: Flask, asyncio, logging, aiofiles, pytz, smtplib, TigerOpen API SDK
```

## Installation

```bash
git clone https://github.com/Joezhou1211/QuantSystem.git
cd QuantSystem
```

##  Setting Up TigerOpen API

Obtain the private key from Tiger Quant:  https://quant.itigerup.com/openapi/en/python/overview/introduction.html

Place the private key in the appropriate directory (e.g., /home/admin/key.pem).

## App Screen Shot (In Chinese only)
<img src="https://github.com/Joezhou1211/QuantSystem/assets/121386280/1788d333-5814-4028-9659-d51f3ab9c0b9" width="400">

<img src="https://github.com/Joezhou1211/QuantSystem/assets/121386280/ce2e1943-d2a8-4c08-bd47-842ae9f9b9db" width="600">

# Simplified Chinese
# 量化系统
这是一个量化交易系统，使用Python实现。该系统基于TigerOpen API，通过接收Tradingview传递的交易信号来执行买卖单和更新本地数据。运行该系统需要挂载该文件在服务器中。


## 提示

- 作者没有购买tiger实时数据，所以无法检测盘中盘后状态。不过我曲线救国，编写了根据服务器时间进入不同交易状态的method并每5分钟校对一次，可能需要根据夏令时重新校准。
- 还是同一个问题，没有实时数据的情况下，实现获取实时价格改单的方式是每分钟使用TradingView发送价格信号，在TradingView通过webhook每分钟发送一次需要监控的标的价格变动。对于初级付费用户，最多可以同时建立20个交易信号，20个价格信号。也就是说，这个系统最多一次自动化交易20个个股。
- 这是作者在大一时写的学习项目，虽然系统已经经过6个月压力测试，且可以确定盘中交易99.9%不会发生问题，但如果需要进入生产环境使用，请确保检查所有细节并了解每一个函数和变量的定义。

- 目前正在编写一套半自动Python图形交易系统，用于日内交易，功能包括分型识别/预设形态选股/自动追踪调整挂买价格/实时新闻监控 + 大模型给出决策建议等。

## 系统特点


低中频交易：支持分钟级别交易，自动挂单/改单/撤单。高于每分钟/次的情况下，容易出现问题。Python不支持并发，如需高频交易，需更改为其他代码。

最佳部署位置：建议将服务器布置在美国华盛顿周边，以优化美股交易的延迟。

灵活集成：如需跨过Tradingview，可考虑使用vector bt或其他开源框架作为策略端和回测端，但需要妥善解决实时数据问题。

自动化功能：系统具备自动下单、改单、撤单、监控和通知功能。当前功能涉及盘后交易，改单基于成交量决定，如果不需要盘后单，可以修改place_order函数禁用该功能。


## 功能特性


市场状态管理：自动更新市场状态，考虑夏令时和冬令时。

订单管理：处理并发数据，管理订单队列，确保订单执行和修改的正确性。

实时数据处理：集成TigerOpen API以获取实时市场数据和订单状态更新。

日志记录和邮件通知：详细的日志记录和关键事件的邮件通知。

CSV记录：记录所有交易和持仓到CSV文件，以供分析和可视化。


## 先决条件
```
Python 3.7+
所需库：Flask、asyncio、logging、aiofiles、pytz、smtplib、TigerOpen API SDK
```
## 安装
```
git clone https://github.com/Joezhou1211/QuantSystem.git
cd QuantSystem
```
## 设置TigerOpen API

从老虎量化获取私钥 https://quant.itigerup.com/openapi/en/python/overview/introduction.html

将私钥放置在适当的目录（如/home/admin/key.pem）

## 软件截图
<img src="https://github.com/Joezhou1211/QuantSystem/assets/121386280/1788d333-5814-4028-9659-d51f3ab9c0b9" width="400">

<img src="https://github.com/Joezhou1211/QuantSystem/assets/121386280/ce2e1943-d2a8-4c08-bd47-842ae9f9b9db" width="600">


