from project.bootstrap import run_database_bootstrap, run_research_bootstrap


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(message % args if args else message)

    def error(self, message, *args):
        self.messages.append(message % args if args else message)


class FakePostgres:
    def __init__(self):
        self.next_id = 1
        self.startup_rows = {}
        self.research_rows = {}

    def test_connection(self):
        self.connected = True

    def execute(self, sql, params=None):
        self.last_execute = sql

    def fetch_all(self, sql, params=None):
        if "INSERT INTO startup_test" in sql:
            row = (self.next_id, "now")
            self.startup_rows[self.next_id] = row
            self.next_id += 1
            return [row]
        if "FROM startup_test" in sql:
            return [self.startup_rows[params[0]]]
        if "INSERT INTO research.strategy_results" in sql:
            row = (self.next_id,)
            self.research_rows[self.next_id] = row
            self.next_id += 1
            return [row]
        if "FROM research.strategy_results" in sql:
            return [self.research_rows[params[0]]]
        return []


def test_database_bootstrap_logs_required_steps():
    postgres = FakePostgres()
    logger = FakeLogger()

    run_database_bootstrap(postgres, logger)

    assert logger.messages == [
        "PostgreSQL connection OK",
        "startup_test created",
        "startup_test insert OK",
        "startup_test read OK",
        "Database bootstrap SUCCESS",
    ]


def test_research_bootstrap_writes_and_reads_result():
    postgres = FakePostgres()
    logger = FakeLogger()

    run_research_bootstrap(postgres, logger)

    assert logger.messages == ["Research write OK", "Research read OK"]
