from composer.cli.console_autoprove import main as wrapped_main
from ai_autoprover.autosetup.handler import install_handler

def main() -> int:
    install_handler()
    return wrapped_main()
