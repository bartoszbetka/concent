from enum import Enum
from enum import unique


@unique
class HashingAlgorithm(Enum):
    SHA1 = 'sha1'

    @staticmethod
    def values() -> list:
        return [algorithm.value for algorithm in HashingAlgorithm]
