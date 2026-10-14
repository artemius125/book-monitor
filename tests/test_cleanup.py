import pytest
from monitor.fetch import Fetcher
from monitor.config import Settings
from monitor.store import Store


async def test_shutdown_after_closed_browser_pipe(tmp_path):
    cfg=Settings(_env_file=None,data_dir=tmp_path)
    cfg.prepare()
    fetcher=Fetcher(cfg,Store(tmp_path))
    class ClosedBrowser:
        async def close(self):
            raise RuntimeError("pipe already closed")
    class ClosedDriver:
        async def stop(self):
            raise RuntimeError("driver already stopped")
    fetcher.browser=ClosedBrowser()
    fetcher.pw=ClosedDriver()
    await fetcher.close()
