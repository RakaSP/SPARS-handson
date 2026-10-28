from ..base.baseline import BaselineScheduler


class FCFSBaseline(BaselineScheduler):
    """First come, first served: jobs are tried in arrival order."""

    def order(self, jobs):
        return jobs
