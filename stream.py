"""
stream.py - real-time feeds from Alpaca (websockets), turned into one simple event queue.

Three feeds, each in its own supervised thread that reconnects by itself:
  bars          every completed 1-minute bar for your universe (free IEX feed, up to 30 symbols)
  news          Alpaca's live news wire (the bot only reacts to stocks it holds or shortlists)
  trade_updates your own paper account: fills, cancels, rejects, stop and target hits

Everything arrives as a plain tuple on `events`, and ONLY the bot's main loop reads it, so there are no
races between the feeds and the trading logic:
    ("bar", symbol, {...})   ("news", {...})   ("trade", {...})
The old 20-second polling stays on as the safety net: if a feed dies or never connects, the bot is
simply slower, not blind. `status()` tells you which feeds have actually delivered recently.
"""
import logging
import threading
import time

log = logging.getLogger("stream")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def bar_event(bar):
    return ("bar", bar.symbol, {"time": bar.timestamp, "open": float(bar.open), "high": float(bar.high),
                                "low": float(bar.low), "close": float(bar.close), "volume": float(bar.volume)})


def news_event(n):
    return ("news", {"id": str(n.id), "headline": str(n.headline), "symbols": [str(s).upper() for s in (n.symbols or [])],
                     "time": n.created_at, "summary": str(getattr(n, "summary", "") or "")[:300]})


def trade_event(u):
    o = u.order
    val = lambda x: str(getattr(x, "value", x)).lower()
    return ("trade", {"event": val(u.event), "symbol": str(o.symbol), "order_id": str(o.id), "side": val(o.side),
                      "order_type": val(o.order_type), "status": val(o.status), "qty": _num(u.qty) or _num(o.qty),
                      "filled_qty": _num(o.filled_qty), "price": _num(u.price), "avg_price": _num(o.filled_avg_price),
                      "time": u.timestamp})


class StreamHub:
    def __init__(self, key, secret, symbols, events, paper=True, feeds=("bars", "news", "trade_updates"), factory=None):
        self.key, self.secret, self.symbols, self.events, self.paper = key, secret, list(symbols), events, paper
        self.feeds, self.factory = feeds, factory or self._alpaca_factory
        self.last, self._stop, self._streams = {}, threading.Event(), {}

    # ----- real Alpaca feeds -----
    def _alpaca_factory(self, name, on_event):
        if name == "bars":
            from alpaca.data.live import StockDataStream
            from alpaca.data.enums import DataFeed
            s = StockDataStream(self.key, self.secret, feed=DataFeed.IEX)

            async def h(bar):
                on_event(bar_event(bar))
            s.subscribe_bars(h, *self.symbols)
        elif name == "news":
            from alpaca.data.live import NewsDataStream
            s = NewsDataStream(self.key, self.secret)

            async def h(n):
                on_event(news_event(n))
            s.subscribe_news(h, "*")
        else:
            from alpaca.trading.stream import TradingStream
            s = TradingStream(self.key, self.secret, paper=self.paper)

            async def h(u):
                on_event(trade_event(u))
            s.subscribe_trade_updates(h)
        return s

    # ----- supervision -----
    def _deliver(self, name):
        def on_event(ev):
            self.last[name] = time.time()
            self.events.put(ev)
        return on_event

    def _supervise(self, name):
        delay = 2.0
        while not self._stop.is_set():
            try:
                stream = self.factory(name, self._deliver(name))
                self._streams[name] = stream
                log.info("stream %s connecting", name)
                stream.run()                                       # blocks until the connection ends
                log.warning("stream %s ended", name)
            except Exception as e:
                log.warning("stream %s failed (%s: %s)", name, type(e).__name__, str(e)[:120])
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, 60.0)                           # back off, never hammer the server

    def start(self):
        for name in self.feeds:
            threading.Thread(target=self._supervise, args=(name,), daemon=True, name="stream-" + name).start()

    def stop(self):
        self._stop.set()
        for s in list(self._streams.values()):
            try:
                s.stop()
            except Exception:
                pass

    def status(self, stale_seconds=150):
        """{feed: True/False}: True if it has delivered something within stale_seconds."""
        now = time.time()
        return {n: (n in self.last and now - self.last[n] <= stale_seconds) for n in self.feeds}
