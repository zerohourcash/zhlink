from zhlink import get_balance, is_valid_address


ADDRESS = "Z..."

if ADDRESS == "Z...":
    print("Edit ADDRESS at the top of this file.")
    raise SystemExit(0)

if not is_valid_address(ADDRESS):
    print("Invalid ZHC address.")
    raise SystemExit(1)

print(get_balance(ADDRESS))
