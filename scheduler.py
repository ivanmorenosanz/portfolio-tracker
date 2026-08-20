from apscheduler.schedulers.background import BackgroundScheduler

import services

scheduler = BackgroundScheduler()


def configure_scheduler(sched: BackgroundScheduler = None) -> BackgroundScheduler:
    """Register all background jobs on the given (or module-level) scheduler."""
    sched = sched or scheduler
    sched.add_job(services.run_auto_contributions, "interval", minutes=30,
                  id="auto_contributions", replace_existing=True)
    sched.add_job(services.run_scheduled_expenses, "interval", minutes=30,
                  id="scheduled_expenses", replace_existing=True)
    sched.add_job(services.run_scheduled_incomes, "interval", minutes=30,
                  id="scheduled_incomes", replace_existing=True)
    sched.add_job(services.run_scheduled_transfers, "interval", minutes=30,
                  id="scheduled_transfers", replace_existing=True)
    sched.add_job(services.refresh_prices, "interval", minutes=5,
                  id="price_refresh", replace_existing=True)
    return sched
