from time import sleep

import pylier


@pylier.node  # auto instrument
def f1():  # no attributes
    sleep(1)


@pylier.node  # auto instrument
def f2(a: int, b: str) -> str:  # with attributes
    f1()
    return f"{a} {b}"


def main():
    while True:
        with pylier.trace("main"):
            i = input("Enter a number: ")
            if i == "q":
                break

            f2(int(i), "hello")


if __name__ == "__main__":
    main()
