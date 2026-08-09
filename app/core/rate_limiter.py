import time
from collections import defaultdict
from typing import Dict, List


class RateLimiter:
    """
    クライアントごとのリクエスト頻度を制限する、インメモリのスライディングウィンドウ実装。

    Renderの本サービスはWEB_CONCURRENCY=1(単一プロセス)で動いているため、
    インメモリ状態で十分機能する。複数プロセス/複数インスタンスに拡張する場合は
    Redis等の共有ストアに置き換える必要がある(その際はこのクラスのインターフェースは
    そのまま使えるよう意図している)。

    海外AIクライアントからの無限連打(DoS)や、カーディング攻撃(少額見積を大量発行して
    Auth可否だけを見て盗難カードの有効性を検証する手口)への一次防御として使う。
    """

    def __init__(self):
        self._requests: Dict[str, List[float]] = defaultdict(list)

    def check(self, client_id: str, max_requests: int, window_seconds: float) -> bool:
        """
        window_seconds以内のリクエスト数がmax_requests以下ならTrue(許可)、
        超えていればFalse(拒否)を返す。呼ぶたびに現在時刻を1リクエストとして記録する。
        """
        now = time.monotonic()
        window_start = now - window_seconds

        timestamps = self._requests[client_id]
        # 古いタイムスタンプを掃除
        while timestamps and timestamps[0] < window_start:
            timestamps.pop(0)

        if len(timestamps) >= max_requests:
            return False

        timestamps.append(now)
        return True

    def reset(self, client_id: str) -> None:
        self._requests.pop(client_id, None)
