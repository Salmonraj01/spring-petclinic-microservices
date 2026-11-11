def divide(numerator: int, denominator: int) -> float:
    # Intentional test defect: zero is not handled.
    return numerator / denominator

if __name__ == "__main__":
    print(divide(10, 0))
