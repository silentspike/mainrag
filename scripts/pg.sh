#!/bin/bash
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag "$@"
