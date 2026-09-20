"""Explicitly initialize a fresh TaskPe database.

This command is allowed to create schema, apply compatibility repairs, and
provision the initial administrator. It must be run against a deliberate
database path after backup/validation; the web application never invokes it
in production.
"""

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True, help='Database path to initialize')
    args = parser.parse_args()
    # The application requires an explicit production path. Set the same
    # deliberate path before importing the database module for this CLI.
    os.environ['TASKPE_DB_PATH'] = str(Path(args.db).resolve())
    import database
    database.DB_PATH = str(Path(args.db).resolve())
    database.initialize_database()
    database.verify_required_schema()
    print('Database initialized and schema verified:', database.DB_PATH)


if __name__ == '__main__':
    main()
