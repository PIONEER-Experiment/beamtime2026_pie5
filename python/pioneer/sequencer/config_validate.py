from __future__ import annotations
from dataclasses import dataclass
import time
from midas.sequencer import SequenceClient

@dataclass
class ODBRequirement:
    seq : SequenceClient
    path : str
    op   : str
    target : float
    upper  : float | None = None
    stable_for : float | None = None
    timeout : float | None = None
    first_check   : float | None = None
    first_success : float | None = None

    def check_now(self):
        val = self.seq.odb_get(self.path)
        if self.op == "==":
            return self.target == val
        elif self.op == "between":
            if self.upper is None:
                raise ValueError("`between` requires `upper`")
            elif self.target > self.upper:
                raise ValueError(f"`between` requires target <= upper, got target = {self.target}, upper = {self.upper}")
            return self.target <= val <= self.upper
        else:
            raise NotImplementedError(f"Operator {self.op} not yet implemented for `check`")

    def check(self):
        t_now = time.monotonic()
        if self.first_check is None:
            self.first_check = t_now

        is_success = self.check_now()

        if self.stable_for is not None:
            if is_success:
                if self.first_success is None:
                    self.first_success = t_now
                elif t_now - self.first_success >= self.stable_for:
                    return True
            else:
                self.first_success = None
        elif is_success:
            return True

        if (self.timeout is not None
            and self.timeout > 0
            and t_now - self.first_check >= self.timeout
            ):
            raise TimeoutError(f"Waiting for {self.path} timed out")

        return False

    def wait(self):
        self.seq.wait_odb(self.path, self.op, self.target, self.upper, self.stable_for, self.timeout)

@dataclass
class ODBRequirementCollection:
    seq : SequenceClient
    name : str
    requirements : list[ODBRequirement | ODBRequirementCollection]
    stable_for : float | None = None
    timeout : float | None = None
    first_check   : float | None = None
    first_success : float | None = None

    def check(self):
        t_now = time.monotonic()
        if self.first_check is None:
            self.first_check = t_now

        succeeded = [r.check() for r in self.requirements]
        is_success = all(succeeded)
        if self.stable_for is not None:
            if is_success:
                if self.first_success is None:
                    self.first_success = t_now
                if t_now - self.first_success >= self.stable_for:
                    return True
            else:
                self.first_success = None
        elif is_success:
            return True
        if (self.timeout is not None
            and self.timeout > 0
            and t_now - self.first_check >= self.timeout
            ):
            raise TimeoutError(f"Waiting for {self.name} timed out")
        return False

    def wait(self):
        self.seq.wait_func(self.check)


