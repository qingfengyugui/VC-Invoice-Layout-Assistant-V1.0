"""Entry point for the complete platform runtime bundle."""

from invoice_layout.runtime import configure_native_environment

configure_native_environment()

from invoice_layout.cli import app

app()
