import os
import re
import json
import time
import requests
from fast_flights import FlightData, Passengers, get_flights

# ================= 配置区域 =================
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
FAKE_PRICE = os.environ.get("FAKE_PRICE")

# v2.2 支持 fetch_mode： "fallback" 使用 Playwright 后备，"common" 直接请求
FETCH_MODE = os.environ.get("FETCH_MODE", "fallback")

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))

ROUTES_TO_MONITOR = [
    {
        "from_airport": "PEK",
        "to_airport": "SHA",
        "date": "2026-10-01",
        "price_threshold": 800,  # 目标阈值（最高心理价）
        "drop_amount": 100,  # 方案C：比历史最低价再降多少才触发
        "trip": "one-way",
        "seat": "economy",
        # 不设置 max_stops -> 返回全部航班（含中转）
    },
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
    if DRY_RUN:
        print("\n" + "=" * 60)
        print("[DRY RUN] 本应推送：")
        print("标题:", title)
        print("-" * 60)
        print(content)
        print("=" * 60 + "\n")
        return True
    if not SERVERCHAN_SENDKEY:
        print("未设置 SERVERCHAN_SENDKEY，跳过推送")
        return False
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send"
    try:
        r = requests.post(url, data={"title": title, "desp": content}, timeout=15)
        print("推送响应:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("推送异常:", e)
        return False


def evaluate_notification(current_price, route, entry):
    """方案C：触发价 = min(目标阈值, 历史最低价 - 降价幅度)"""
    threshold = route["price_threshold"]
    drop_amount = route.get("drop_amount", 0)

    lowest_seen = entry.get("lowest_seen_price") if entry else None
    last_notified = entry.get("last_notified_price") if entry else None

    if lowest_seen is None:
        trigger_price = threshold
    else:
        trigger_price = min(threshold, lowest_seen - drop_amount)

    if last_notified is None:
        if current_price <= trigger_price:
            return True, f"首次低于触发价 {trigger_price}（当前 {current_price}）"
        return False, ""

    if current_price <= trigger_price and current_price < last_notified:
        if lowest_seen is not None and lowest_seen - drop_amount < threshold:
            reason = f"低于动态触发价 {trigger_price}（历史最低 {lowest_seen} - {drop_amount}）"
        else:
            reason = f"低于目标阈值 {trigger_price}"
        return True, reason

    return False, ""


def fetch_current_price(route):
    """获取当前最低价，返回 (current_price, cheapest_flight, summary)"""
    if FAKE_PRICE:
        current_price = int(FAKE_PRICE)
        cheapest = type(
            "FakeFlight",
            (),
            {
                "price": f"${current_price}",
                "name": "Mock Airline",
                "departure": "08:00",
                "arrival": "10:30",
                "stops": 0,
            },
        )()
        print(f"[MOCK] 使用模拟价格: {current_price}")
        return current_price, cheapest, "Mock 数据"

    result = None
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            if attempt > 0:
                print(f"第 {attempt + 1} 次尝试...")
                time.sleep(3)
            # v2.2 API：直接传 flight_data，支持 fetch_mode
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
                fetch_mode=FETCH_MODE,
            )
            break
        except IndexError as e:
            last_error = f"解析错误 (IndexError): {e}"
            print(f"尝试 {attempt + 1}/{MAX_RETRIES + 1} 失败: {last_error}")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"尝试 {attempt + 1}/{MAX_RETRIES + 1} 失败: {last_error}")

    if result is None:
        print(f"获取航班最终失败: {last_error}")
        return None, None, None

    if not result or not getattr(result, "flights", None):
        print("没有航班数据")
        return None, None, None

    valid = []
    for f in result.flights:
        price = parse_price(getattr(f, "price", ""))
        if price is not None:
            valid.append((price, f))

    if not valid:
        print("没有可解析的价格")
        return None, None, None

    direct_count = sum(1 for _, f in valid if getattr(f, "stops", 0) == 0)
    connecting_count = len(valid) - direct_count
    summary = f"共 {len(valid)} 个航班（直飞 {direct_count}，中转 {connecting_count}）"

    current_price, cheapest = min(valid, key=lambda x: x[0])
    return current_price, cheapest, summary


def check_route(route, state):
    route_key = (
        f"{route['from_airport']}-{route['to_airport']}-"
        f"{route['date']}-{route.get('seat', 'economy')}"
    )
    print(f"\n检查航线: {route_key}")

    current_price, cheapest, summary = fetch_current_price(route)
    if current_price is None:
        return

    print(f"当前最低价: {current_price}")
    if summary:
        print(f"航班统计: {summary}")

    entry = state.get(route_key, {})
    should, reason = evaluate_notification(current_price, route, entry)

    lowest_seen = entry.get("lowest_seen_price")
    if lowest_seen is None or current_price < lowest_seen:
        lowest_seen = current_price

    if should:
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
**目标阈值**: {route['price_threshold']}
**降价幅度**: {route.get('drop_amount', 0)}
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
    print(f">>> FETCH_MODE = {FETCH_MODE} <<<")

    state = load_state()
    for route in ROUTES_TO_MONITOR:
        check_route(route, state)
        if not FAKE_PRICE:
            time.sleep(3)
    save_state(state)
    print("\n完成。当前状态已保存到", STATE_FILE)


if __name__ == "__main__":
    main()
