ADDRESS = "Z..."


def main() -> None:
    if ADDRESS == "Z...":
        print("Edit ADDRESS at the top of this file.")
        return

    from zhlink import get_balance

    print(get_balance(ADDRESS))


if __name__ == "__main__":
    main()
