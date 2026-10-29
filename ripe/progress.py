"""In-memory progress of one update check; read by /api/poll/status, never persisted or polled from the database."""
from __future__ import annotations
from collections import deque
from .models import utcnow

PHASES = ('collecting', 'analysing', 'summarising', 'done')
ETA_MIN_CALLS = 3
ETA_WINDOW = 10
MAX_ERRORS = 10


class PollProgress:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running = False
        self.phase = 'done'
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.sources = {'done': 0, 'total': 0}
        self.releases = {'done': 0, 'total': 0, 'skipped': 0}
        self.current = ''
        self.last_error: str | None = None
        self.errors: list[dict] = []
        self.durations: deque[float] = deque(maxlen=ETA_WINDOW)
        self.calls = 0

    def start(self):
        self.reset()
        self.running = True
        self.phase = 'collecting'
        self.started_at = utcnow().isoformat()

    # Collecting
    def add_sources(self, count: int):
        self.sources['total'] += count

    def source_done(self, name: str = ''):
        self.sources['done'] += 1
        self.sources['total'] = max(self.sources['total'], self.sources['done'])
        self.current = name

    def working_on(self, name: str):
        self.current = name

    def add_errors(self, errors: list[dict]):
        for item in errors:
            if len(self.errors) < MAX_ERRORS:
                self.errors.append({'source': str(item.get('source', '')), 'message': str(item.get('message', ''))})

    def end_collecting(self):
        self.sources['done'] = self.sources['total'] = max(self.sources['total'], self.sources['done'])
        self.current = ''

    # Analysing
    def begin_analysis(self, total: int, skipped: int = 0):
        self.phase = 'analysing'
        self.releases = {'done': 0, 'total': total, 'skipped': skipped}

    def release_done(self, name: str = '', seconds: float | None = None):
        """Count one queued release; seconds is given only when a model call was made."""
        self.releases['done'] = min(self.releases['done'] + 1, self.releases['total'])
        if name:
            self.current = name
        if seconds is not None:
            self.durations.append(seconds)
            self.calls += 1

    # Summarising / end
    def begin_summaries(self):
        self.phase = 'summarising'
        self.current = ''

    def finish(self, error: str | None = None):
        self.running = False
        self.phase = 'done'
        self.current = ''
        self.finished_at = utcnow().isoformat()
        self.last_error = error

    def eta_seconds(self) -> int | None:
        if not self.running or self.phase != 'analysing' or self.calls < ETA_MIN_CALLS or not self.durations:
            return None
        remaining = self.releases['total'] - self.releases['done']
        return max(0, round(sum(self.durations) / len(self.durations) * remaining))

    def snapshot(self) -> dict:
        return {'running': self.running, 'phase': self.phase, 'started_at': self.started_at,
                'finished_at': self.finished_at, 'sources': dict(self.sources), 'releases': dict(self.releases),
                'current': self.current, 'eta_seconds': self.eta_seconds(), 'last_error': self.last_error,
                'errors': list(self.errors)}
