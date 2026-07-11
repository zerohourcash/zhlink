from zhlink import get_balance


ADDRESS = "Z..."

if ADDRESS == "Z...":
    print("Edit ADDRESS at the top of this file.")
    raise SystemExit(0)

print(get_balance(ADDRESS))
