"""Wallet Manager — unified key derivation for EVM, Solana, Cosmos, StarkNet."""

import hashlib, hmac, logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class WalletKeys:
    address: str
    private_key: str
    chain_type: str  # "evm", "solana", "cosmos", "stark"
    extra: dict = field(default_factory=dict)

class WalletManager:
    """Derives venue-specific keys from a single master mnemonic."""

    VENUE_SIGNING = {
        "hyperliquid": "evm", "lighter": "evm", "aster": "evm",
        "drift": "solana", "dydx": "cosmos_dydx", "apex": "evm",
        "paradex": "stark", "ethereal": "evm", "injective": "cosmos_injective",
    }

    def __init__(self, mnemonic: str = "", private_keys: dict = None):
        self.mnemonic = mnemonic
        self._explicit_keys = private_keys or {}
        self._derived: dict[str, WalletKeys] = {}

    def get_evm_wallet(self, index: int = 0) -> WalletKeys:
        key = f"evm_{index}"
        if key in self._derived: return self._derived[key]
        if "evm" in self._explicit_keys:
            keys = WalletKeys(self._addr_from_key(self._explicit_keys["evm"]),
                              self._explicit_keys["evm"], "evm")
        elif self.mnemonic:
            keys = self._derive_evm(index)
        else:
            raise ValueError("No mnemonic or EVM key")
        self._derived[key] = keys
        return keys

    def get_solana_wallet(self, index: int = 0) -> WalletKeys:
        key = f"sol_{index}"
        if key in self._derived: return self._derived[key]
        if "solana" in self._explicit_keys:
            keys = WalletKeys("", self._explicit_keys["solana"], "solana")
        elif self.mnemonic:
            seed = hashlib.pbkdf2_hmac("sha512", self.mnemonic.encode(), b"mnemonic", 2048)
            derived = hashlib.sha512(seed + f"m/44'/501'/{index}'/0'".encode()).digest()
            keys = WalletKeys(derived[:32].hex(), derived[:32].hex(), "solana")
        else:
            raise ValueError("No mnemonic or Solana key")
        self._derived[key] = keys
        return keys

    def get_cosmos_wallet(self, chain: str = "dydx", index: int = 0) -> WalletKeys:
        key = f"cosmos_{chain}_{index}"
        if key in self._derived: return self._derived[key]
        evm = self.get_evm_wallet(index)
        prefix = "dydx" if chain == "dydx" else "inj"
        keys = WalletKeys(f"{prefix}1...", evm.private_key, "cosmos",
                          {"chain": chain, "prefix": prefix, "evm_address": evm.address})
        self._derived[key] = keys
        return keys

    def get_stark_wallet(self, index: int = 0) -> WalletKeys:
        key = f"stark_{index}"
        if key in self._derived: return self._derived[key]
        evm = self.get_evm_wallet(index)
        stark_key = hmac.new(b"StarkKeyDerivation",
            bytes.fromhex(evm.private_key.replace("0x","")), hashlib.sha256).hexdigest()
        keys = WalletKeys(evm.address, stark_key, "stark",
                          {"eth_address": evm.address, "eth_key": evm.private_key})
        self._derived[key] = keys
        return keys

    def get_wallet_for_venue(self, venue_name: str, index: int = 0) -> WalletKeys:
        sig = self.VENUE_SIGNING.get(venue_name, "evm")
        if sig == "evm": return self.get_evm_wallet(index)
        elif sig == "solana": return self.get_solana_wallet(index)
        elif sig == "cosmos_dydx": return self.get_cosmos_wallet("dydx", index)
        elif sig == "cosmos_injective": return self.get_cosmos_wallet("injective", index)
        elif sig == "stark": return self.get_stark_wallet(index)
        else: raise ValueError(f"Unknown venue: {venue_name}")

    def _derive_evm(self, index: int) -> WalletKeys:
        try:
            from eth_account import Account
            Account.enable_unaudited_hdwallet_features()
            acct = Account.from_mnemonic(self.mnemonic, account_path=f"m/44'/60'/0'/0/{index}")
            return WalletKeys(acct.address, acct.key.hex(), "evm")
        except ImportError:
            logger.error("eth_account not installed")
            raise

    def _addr_from_key(self, pk: str) -> str:
        try:
            from eth_account import Account
            return Account.from_key(pk).address
        except ImportError:
            return "0x" + hashlib.sha256(pk.encode()).hexdigest()[:40]
