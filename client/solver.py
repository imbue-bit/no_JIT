import yaml
import time
from web3 import Web3
from scipy.optimize import minimize_scalar

POOL_MANAGER_ABI = [
    {"inputs": [{"internalType": "PoolId", "name": "id", "type": "bytes32"}], "name": "getLiquidity", "outputs": [{"internalType": "uint128", "name": "liquidity", "type": "uint128"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "PoolId", "name": "id", "type": "bytes32"}], "name": "getSlot0", "outputs": [{"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"}, {"internalType": "int24", "name": "tick", "type": "int24"}, {"internalType": "uint16", "name": "protocolFee", "type": "uint16"}, {"internalType": "uint24", "name": "lpFee", "type": "uint24"}], "stateMutability": "view", "type": "function"}
]

HOOK_ABI = [
    {"inputs": [{"components": [{"internalType": "uint128", "name": "thresholdRatioBps", "type": "uint128"}, {"internalType": "uint24", "name": "feePips", "type": "uint24"}], "internalType": "struct JITDefenseHook.FeeTier[]", "name": "_newTiers", "type": "tuple[]"}], "name": "setFeeTiers", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]

class ProductionHJISolver:
    def __init__(self, config_path):
        with open(config_path, 'r') as f:
            self.cfg = yaml.safe_load(f)
        
        self.w3 = Web3(Web3.HTTPProvider(self.cfg['network']['rpc_url']))
        self.pm = self.w3.eth.contract(address=self.cfg['contracts']['pool_manager'], abi=POOL_MANAGER_ABI)
        self.hook = self.w3.eth.contract(address=self.cfg['contracts']['hook_address'], abi=HOOK_ABI)
        self.pool_id = self.cfg['contracts']['pool_id']
        self.acct = self.w3.eth.account.from_key(self.cfg['network']['governor_private_key'])

    def get_realtime_state(self):
        liquidity = self.pm.functions.getLiquidity(self.pool_id).call()
        slot0 = self.pm.functions.getSlot0(self.pool_id).call()
        gas_price = self.w3.eth.gas_price
        return liquidity, slot0[1], gas_price

    def solve_phi_crit(self, ratio_bps, L_active, gas_price, v_swap):
        """求解 HJI 临界点"""
        alpha = (L_active * ratio_bps) / 10000
        c_gas_eth = (gas_price * self.cfg['market_assumptions']['jit_gas_usage']) / 1e18        
        def max_profit_for_phi(phi):
            res = minimize_scalar(
                lambda a: -(phi * (a / (L_active + a)) * v_swap - (c_gas_eth + 0.5 * self.cfg['market_assumptions']['kappa'] * a**2)),
                bounds=(alpha * 0.8, alpha * 1.2),
                method='bounded'
            )
            return -res.fun
        low, high = 0.0005, 0.1 # 5bps to 1000bps
        for _ in range(20):
            mid = (low + high) / 2
            if max_profit_for_phi(mid) > 0: low = mid
            else: high = mid
        return high

    def sync_to_chain(self):
        L, tick, gp = self.get_realtime_state()
        if L == 0: return
        
        tiers_payload = []
        for r in self.cfg['strategy']['ratio_tiers']:
            phi = self.solve_phi_crit(r, L, gp, self.cfg['market_assumptions']['v_swap_nominal'])
            tiers_payload.append({
                "thresholdRatioBps": r,
                "feePips": int(phi * 1_000_000)
            })
        
        tx = self.hook.functions.setFeeTiers(tiers_payload).build_transaction({
            'from': self.acct.address,
            'nonce': self.w3.eth.get_transaction_count(self.acct.address),
            'gas': 500000,
            'maxFeePerGas': int(gp * 1.2),
            'maxPriorityFeePerGas': self.w3.eth.max_priority_fee
        })
        signed_tx = self.w3.eth.account.sign_transaction(tx, self.acct.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        print(f"Strategy updated. L_active: {L}, Tick: {tick}, Tx: {tx_hash.hex()}")

if __name__ == "__main__":
    solver = ProductionHJISolver("config.yaml")
    while True:
        try:
            solver.sync_to_chain()
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(60)