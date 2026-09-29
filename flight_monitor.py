import os
import re
import json
import time
import requests
from fast_flights import FlightData, Passengers, get_flights

# ================= 配置区域 =================
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# 测试开关
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"       # 1=只打印不推送
FAKE_PRICE = os.environ.get("FAKE_PRICE")              # 设置后跳过真实请求，用此价格模拟

ROUTES_TO_MONITOR = [
    {
        "from_airport": "PEK",
        "to_airport": "SHA",
        "date": "2026-10-01",
        "price_threshold": 800,      # 目标阈值
        "threshold_margin": 50,       # 低于阈值多少才触发
        "drop_amount": 100,           # 较上次推送降价达到此值就触发
        "trip": "one-way",
        "seat": "economy",
    },
    # 可以继续添加更多航线
]
# ===========================================


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_price(price_str):
    m = re.search(r"([\d,]+)", str(price_str))
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def send_wechat(title, content):
    """发送微信推送；DRY_RUN 模式下只打印不发送"""
    if DRY_RUN:
        print("\n" + "=" * 60)
        print("[DRY RUN] 本应推送：")
        print("标题:", title)
        print("-" * 60)
        print(content)
        print("=" * 60 + "\n")
        return True  # 返回 True 让 state 正常更新，模拟真实推送

    if not SERVERCHAN_SENDKEY:
        print("未设置 SERVERCHAN_SENDKEY，跳过推送")
        return False

    url = f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send"
    try:
        r = requests.post(
            url,
            data={"title": title, "desp": content},
            timeout=15,
        )
        print("推送响应:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("推送异常:", e)
        return False


def evaluate_notification(current_price, route, entry):
    """判断是否推送，返回 (should_notify, reason)"""
    threshold = route["price_threshold"]
    margin = route.get("threshold_margin", 0)
    drop_amount = route.get("drop_amount", 0)
    trigger_price = threshold - margin

    last_notified = entry.get("last_notified_price") if entry else None

    if last_notified is None:
        if current_price <= trigger_price:
            return True, f"首次低于触发价 {trigger_price}（当前 {current_price}）"
        return False, ""

    if current_price <= trigger_price and current_price < last_notified:
        return True, f"低于触发价且创新低（较上次推送降 {last_notified - current_price}）"

    if drop_amount > 0 and current_price <= last_notified - drop_amount:
        return True, f"较上次推送降价 {last_notified - current_price}（达到阈值 {drop_amount}）"

    return False, ""


def fetch_current_price(route):
    """获取当前最低价，返回 (current_price, cheapest_flight) 或 (None, None)"""
    # 模拟模式：直接返回假数据
    if FAKE_PRICE:
        current_price = int(FAKE_PRICE)
        cheapest = type("FakeFlight", (), {
            "price": f"${current_price}",
            "name": "Mock Airline",
            "departure": "08:00",
            "arrival": "10:30",
            "stops": 0,
        })()
        print(f"[MOCK] 使用模拟价格: {current_price}")
        return current_price, cheapest

    # 真实请求
    try:
        result = get_flights(
            flight_data=[
                FlightData(
                    date=route["date"],
                    from_airport=route["from_airport"],
                    to_airport=route["to_airport"],
                )
            ],
            trip=route.get("trip", "one-way"),
            seat=route.get("seat", "economy"),
            passengers=Passengers(adults=1),
            fetch_mode="fallback",
        )
    except Exception as e:
        print(f"获取航班失败: {e}")
        return None, None

    if not result or not getattr(result, "flights", None):
        print("没有航班数据")
        return None, None

    valid = []
    for f in result.flights:
        price = parse_price(getattr(f, "price", ""))
        if price is not None:
            valid.append((price, f))

    if not valid:
        print("没有可解析的价格")
        return None, None

    return min(valid, key=lambda x: x[0])


def check_route(route, state):
    route_key = (
        f"{route['from_airport']}-{route['to_airport']}-"
        f"{route['date']}-{route.get('seat', 'economy')}"
    )
    print(f"\n检查航线: {route_key}")

    current_price, cheapest = fetch_current_price(route)
    if current_price is None:
        return

    print(f"当前最低价: {current_price}")

    entry = state.get(route_key, {})
    should, reason = evaluate_notification(current_price, route, entry)

    lowest_seen = entry.get("lowest_seen_price")
    if lowest_seen is None or current_price < lowest_seen:
        lowest_seen = current_price

    if should:
        trigger_price = route["price_threshold"] - route.get("threshold_margin", 0)
        title = f"✈️ 机票低价: {route['from_airport']} → {route['to_airport']}"
        content = f"""**航线**: {route['from_airport']} → {route['to_airport']}
**日期**: {route['date']}
**当前最低价**: {getattr(cheapest, 'price', '')}（解析: {current_price}）
**历史最低**: {lowest_seen}
**上次推送**: {entry.get('last_notified_price', '无')}
**触发原因**: {reason}
**航班**: {getattr(cheapest, 'name', '未知')}
**起飞**: {getattr(cheapest, 'departure', '未知')}
**到达**: {getattr(cheapest, 'arrival', '未知')}
**经停**: {getattr(cheapest, 'stops', '未知')}
**目标阈值**: {route['price_threshold']}（触发价: {trigger_price}）
"""
        if send_wechat(title, content):
            state[route_key] = {
                "last_notified_price": current_price,
                "lowest_seen_price": lowest_seen,
                "last_checked_price": current_price,
                "notify_count": entry.get("notify_count", 0) + 1,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            print(f"已记录推送状态: {reason}")
    else:
        state[route_key] = {
            "last_notified_price": entry.get("last_notified_price"),
            "lowest_seen_price": lowest_seen,
            "last_checked_price": current_price,
            "notify_count": entry.get("notify_count", 0),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        print(
            f"不推送，当前 {current_price}，"
            f"上次推送 {entry.get('last_notified_price', '无')}"
        )


def main():
    if DRY_RUN:
        print(">>> DRY RUN 模式：不会真正发送推送 <<<")
    if FAKE_PRICE:
        print(f">>> MOCK 模式：使用模拟价格 {FAKE_PRICE} <<<")

    state = load_state()
    for route in ROUTES_TO_MONITOR:
        check_route(route, state)
        if not FAKE_PRICE:
            time.sleep(3)  # 真实请求时避免过于频繁
    save_state(state)
    print("\n完成。当前状态已保存到", STATE_FILE)


if __name__ == "__main__":
    main()