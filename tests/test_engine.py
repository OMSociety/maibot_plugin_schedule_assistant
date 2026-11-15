"""调度薄层的触发解析。"""

import logging

from apscheduler.triggers.cron import CronTrigger

from schedule_assistant.engine import TimedMessageEngine


class FakeScheduler:
    def __init__(self):
        self.running = False
        self.jobs = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def remove_job(self, job_id):
        pass

    def start(self):
        self.running = True

    def shutdown(self, wait=False):
        self.running = False


def make_engine():
    return TimedMessageEngine(FakeScheduler(), logging.getLogger("test"))


async def handler():
    return None


def test_register_hhmm_creates_cron_trigger():
    engine = make_engine()
    assert engine.register("morning", "09:00", handler) is True
    trigger = engine.scheduler.jobs[0][1]
    assert isinstance(trigger, CronTrigger)


def test_register_interval():
    engine = make_engine()
    assert engine.register("water", "interval:30", handler) is True
    func, trigger_type, kwargs = engine.scheduler.jobs[0]
    assert trigger_type == "interval" and kwargs["minutes"] == 30


def test_register_invalid_trigger_returns_false():
    engine = make_engine()
    assert engine.register("bad", "not-a-trigger", handler) is False


def test_start_and_shutdown():
    engine = make_engine()
    engine.register("morning", "09:00", handler)
    engine.start()
    assert engine.scheduler.running is True
    engine.shutdown()
    assert engine.scheduler.running is False
