"""Keep analysis and generation within one Stockfish process at a time."""
import threading

engine_slots = threading.BoundedSemaphore(1)
