"""Print the update routing guide (which areas a change can touch). Pure text, no model call."""
from .deps import ROUTING_GUIDE


def main():
    print(ROUTING_GUIDE)


if __name__ == "__main__":
    main()
