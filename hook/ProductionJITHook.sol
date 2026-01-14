pragma solidity ^0.8.26;

import {BaseHook} from "v4-periphery/src/base/hooks/BaseHook.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {Hooks} from "v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {StateLibrary} from "v4-core/src/libraries/StateLibrary.sol";
import {LPFeeLibrary} from "v4-core/src/libraries/LPFeeLibrary.sol";
import {BeforeSwapDelta, BeforeSwapDeltaLibrary} from "v4-core/src/types/BeforeSwapDelta.sol";
import {BalanceDelta, BalanceDeltaLibrary} from "v4-core/src/types/BalanceDelta.sol";

contract ProductionJITHook is BaseHook {
    using PoolIdLibrary for PoolKey;
    using StateLibrary for IPoolManager;

    struct BlockAudit {
        uint128 deltaL;
        uint64 lastBlock;
    }

    struct FeeTier {
        uint128 thresholdRatioBps; 
        uint24 feePips;            
    }

    mapping(PoolId => BlockAudit) public poolAudits;
    FeeTier[] public feeTiers; 
    address public immutable governor;

    constructor(IPoolManager _poolManager, address _governor) BaseHook(_poolManager) {
        governor = _governor;
    }

    function setFeeTiers(FeeTier[] calldata _newTiers) external {
        require(msg.sender == governor, "Auth");
        delete feeTiers;
        for (uint256 i = 0; i < _newTiers.length; i++) {
            feeTiers.push(_newTiers[i]);
        }
    }

    function getHookPermissions() public pure override returns (Hooks.Permissions memory) {
        return Hooks.Permissions({
            beforeInitialize: false,
            afterInitialize: false,
            beforeAddLiquidity: false,
            afterAddLiquidity: true,
            beforeRemoveLiquidity: false,
            afterRemoveLiquidity: false,
            beforeSwap: true,
            afterSwap: false,
            beforeDonate: false,
            afterDonate: false,
            beforeSwapReturnDelta: false,
            afterSwapReturnDelta: false,
            afterAddLiquidityReturnDelta: false,
            afterRemoveLiquidityReturnDelta: false
        });
    }

    function afterAddLiquidity(
        address,
        PoolKey calldata key,
        IPoolManager.ModifyLiquidityParams calldata params,
        BalanceDelta,
        BalanceDelta,
        bytes calldata
    ) external override returns (bytes4, BalanceDelta) {
        PoolId poolId = key.toId();
        (, int24 currentTick, , ) = poolManager.getSlot0(poolId);

        // 严谨逻辑：JIT 必须在当前活跃 Tick 注入
        if (params.tickLower <= currentTick && params.tickUpper >= currentTick) {
            BlockAudit storage audit = poolAudits[poolId];
            if (audit.lastBlock != block.number) {
                audit.lastBlock = uint64(block.number);
                audit.deltaL = 0;
            }
            if (params.liquidityDelta > 0) {
                audit.deltaL += uint128(uint256(params.liquidityDelta));
            }
        }
        return (this.afterAddLiquidity.selector, BalanceDeltaLibrary.ZERO_DELTA);
    }

    function beforeSwap(
        address,
        PoolKey calldata key,
        IPoolManager.SwapParams calldata,
        bytes calldata
    ) external override returns (bytes4, BeforeSwapDelta, uint24) {
        PoolId poolId = key.toId();
        BlockAudit memory audit = poolAudits[poolId];

        if (audit.lastBlock == block.number && audit.deltaL > 0) {
            uint128 L_active = poolManager.getLiquidity(poolId);
            if (L_active > 0) {
                uint256 ratio = (uint256(audit.deltaL) * 10000) / L_active;
                
                for (uint256 i = feeTiers.length; i > 0; i--) {
                    if (ratio >= feeTiers[i-1].thresholdRatioBps) {
                        return (
                            this.beforeSwap.selector,
                            BeforeSwapDeltaLibrary.ZERO_DELTA,
                            feeTiers[i-1].feePips | LPFeeLibrary.OVERRIDE_FEE_FLAG
                        );
                    }
                }
            }
        }
        return (this.beforeSwap.selector, BeforeSwapDeltaLibrary.ZERO_DELTA, 0);
    }
}