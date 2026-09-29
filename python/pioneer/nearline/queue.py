
import pioneer.nearline.jobs as nl_jobs

class NearlineQueue:
    def __init__(self, name : str = None, maxJobs : int = 1):
        self.name : str  = name
        self.maxJobs :int = maxJobs
        self.active : list[nl_jobs.BaseJob] = list()

    def get_finshed(self) -> list[nl_jobs.BaseJob]:
        completed = list()
        for aJob in self.active:
            rc = aJob.poll()
            if rc is None:
                # This job is still running
                continue
            completed.append(aJob)
        for j in completed:
            self.active.remove(j)
        return completed

    def getOpenSlots(self) -> int:
        return self.maxJobs - len(self.active)

    def add(self, aJob : nl_jobs.BaseJob) -> None:
        self.active.append(aJob)