# scripts/reiniciar_cargas.py
"""
Script utilitario para reiniciar todas las cargas previas de ingesta.
Es un punto de entrada equivalente a 'python -m scripts.reset_inventory'.
"""
from scripts.reset_inventory import main

if __name__ == "__main__":
    main()
