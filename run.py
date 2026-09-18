# run.py
# Entrypoint to run either the web UI or the CLI.

import sys

""" USAGE
# Run the web UI
python run.py web_ui

# Run the CLI
python run.py query --name total_spending_by_cardholder

"""

# Imports are deliberately done lazily, inside the branch that needs them,
# so that a failure to import one entrypoint's dependencies cannot break
# the others.


def main(argv=None) -> None:
    argv = sys.argv if argv is None else argv
    command = argv[1] if len(argv) > 1 else ""

    if command == "web_ui":
        from app.web import flask_ui

        flask_ui.run_web_ui()
    else:
        from app.cli import cli

        cli.run_cli()


if __name__ == "__main__":
    main()
