// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.24;

// Test-only platforms, each with its own rules. They run on a local anvil chain, never on BSC.

/// Stand-in for the .ai Agent ID registry; its runtime code is copied to the real registry address.
contract MockAgentId {
    mapping(address => string) internal handles;

    function setHandle(address who, string calldata handle) external {
        handles[who] = handle;
    }

    function handleOf(address who) external view returns (string memory) {
        return handles[who];
    }
}

interface IAgentId {
    function handleOf(address who) external view returns (string memory);
}

abstract contract AgentOnly {
    IAgentId internal constant AGENT_ID = IAgentId(0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F);

    error NotAgent();

    modifier onlyAgent() {
        if (bytes(AGENT_ID.handleOf(msg.sender)).length == 0) revert NotAgent();
        _;
    }

    function isAgent(address who) external view returns (bool) {
        return bytes(AGENT_ID.handleOf(who)).length > 0;
    }
}

/// Per-unit mint: exact price, 5 per wallet, 8 in total.
contract CapMint is AgentOnly {
    uint256 public constant PRICE = 0.0032 ether;
    uint256 public constant MAX_PER_WALLET = 5;
    uint256 public constant MAX_SUPPLY = 8;
    uint256 public totalMinted;
    mapping(address => uint256) public mintedOf;

    error WrongValue();
    error ExceedsWalletCap();
    error SoldOut();

    function mint(uint256 qty) external payable onlyAgent {
        if (qty == 0 || msg.value != PRICE * qty) revert WrongValue();
        if (mintedOf[msg.sender] + qty > MAX_PER_WALLET) revert ExceedsWalletCap();
        if (totalMinted + qty > MAX_SUPPLY) revert SoldOut();
        mintedOf[msg.sender] += qty;
        totalMinted += qty;
    }
}

/// Amount-based buy: at least 0.01 BNB per buy, at most 0.03 BNB per wallet in total.
contract AmountBuy is AgentOnly {
    uint256 public constant MIN_BUY = 0.01 ether;
    uint256 public constant MAX_PER_WALLET = 0.03 ether;
    mapping(address => uint256) public paidOf;
    mapping(address => address) public referrerOf;

    error BelowMin();
    error ExceedsWalletCap();

    function buy(address referrer) external payable onlyAgent {
        if (msg.value < MIN_BUY) revert BelowMin();
        if (paidOf[msg.sender] + msg.value > MAX_PER_WALLET) revert ExceedsWalletCap();
        paidOf[msg.sender] += msg.value;
        referrerOf[msg.sender] = referrer;
    }
}

/// Slot launch: exactly 0.05 BNB, one slot per wallet, 3 slots, refund to any address after the deadline if not full.
contract SlotLaunch is AgentOnly {
    uint256 public constant SLOT_PRICE = 0.05 ether;
    uint256 public constant TOTAL_SLOTS = 3;
    uint256 public immutable deadline;
    uint256 public filled;
    mapping(address => bool) public hasSlot;

    error WrongValue();
    error AlreadyFilled();
    error Full();
    error Expired();
    error NotExpired();
    error NothingToRefund();

    constructor() {
        deadline = block.timestamp + 1 hours;
    }

    function fillSlot() external payable onlyAgent {
        if (block.timestamp > deadline) revert Expired();
        if (msg.value != SLOT_PRICE) revert WrongValue();
        if (hasSlot[msg.sender]) revert AlreadyFilled();
        if (filled == TOTAL_SLOTS) revert Full();
        hasSlot[msg.sender] = true;
        filled += 1;
    }

    function refundTo(address to) external {
        if (block.timestamp <= deadline || filled == TOTAL_SLOTS) revert NotExpired();
        if (!hasSlot[msg.sender]) revert NothingToRefund();
        hasSlot[msg.sender] = false;
        (bool ok,) = to.call{value: SLOT_PRICE}("");
        require(ok);
    }
}

/// Minimum quantity 3, at most 6 per wallet, opens 30 minutes after deployment.
contract MinQtyTimed is AgentOnly {
    uint256 public constant PRICE = 0.001 ether;
    uint256 public constant MIN_QTY = 3;
    uint256 public constant MAX_PER_WALLET = 6;
    uint256 public immutable startTime;
    mapping(address => uint256) public boughtOf;

    error NotStarted();
    error BelowMinQty();
    error ExceedsWalletCap();
    error WrongValue();

    constructor() {
        startTime = block.timestamp + 30 minutes;
    }

    function purchase(uint256 qty) external payable onlyAgent {
        if (block.timestamp < startTime) revert NotStarted();
        if (qty < MIN_QTY) revert BelowMinQty();
        if (boughtOf[msg.sender] + qty > MAX_PER_WALLET) revert ExceedsWalletCap();
        if (msg.value != PRICE * qty) revert WrongValue();
        boughtOf[msg.sender] += qty;
    }
}

/// Presale token: until graduation it only moves to and from its pool.
/// Plain ERC-20 anyone can mint, for transfer tests.
contract MockToken {
    string public constant name = "Mock Token";
    string public constant symbol = "MOCK";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract PresaleToken {
    string public constant name = "Presale Mock";
    string public constant symbol = "PSM";
    uint8 public constant decimals = 18;
    address public immutable pool;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor() {
        pool = msg.sender;
    }

    function mint(address to, uint256 amount) external {
        require(msg.sender == pool, "pool only");
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(msg.sender == pool || to == pool, "locked until graduation");
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(from == pool || to == pool, "locked until graduation");
        allowance[from][msg.sender] -= amount;
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) internal {
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }
}

/// Fixed-price presale pool shaped like the ones agents get pasted instructions for:
/// 0.1429 BNB per 1% of a 1B supply, 1% tax both ways, 30M per agent, 300M for sale.
contract PresalePool is AgentOnly {
    uint256 public constant PRICE_PER_PCT = 0.1429 ether;
    uint256 public constant PCT = 10_000_000 ether;
    uint256 public constant maxPerAgent = 30_000_000 ether;
    uint256 public constant FOR_SALE = 300_000_000 ether;
    PresaleToken public immutable token;
    uint256 public tokensSold;
    bool public graduated;
    mapping(address => uint256) public boughtBy;

    error Graduated();
    error WrongValue();
    error OverLimit();
    error Slippage();

    constructor() {
        token = new PresaleToken();
    }

    function quoteBuy(uint256 amountIn) public pure returns (uint256 tokensOut, uint256 tax) {
        tax = amountIn / 100;
        tokensOut = (amountIn - tax) * PCT / PRICE_PER_PCT;
    }

    function quoteSell(uint256 tokensIn) public pure returns (uint256 amountOut, uint256 tax) {
        uint256 gross = tokensIn * PRICE_PER_PCT / PCT;
        tax = gross / 100;
        amountOut = gross - tax;
    }

    function buyLimit(address wallet) public view returns (uint256) {
        uint256 left = FOR_SALE - tokensSold;
        uint256 mine = maxPerAgent - boughtBy[wallet];
        return left < mine ? left : mine;
    }

    function buy(uint256 amountIn, uint256 minTokensOut) external payable onlyAgent {
        if (graduated) revert Graduated();
        if (msg.value != amountIn) revert WrongValue();
        (uint256 out,) = quoteBuy(amountIn);
        if (out > buyLimit(msg.sender)) revert OverLimit();
        if (out < minTokensOut) revert Slippage();
        boughtBy[msg.sender] += out;
        tokensSold += out;
        token.mint(msg.sender, out);
    }

    function sell(uint256 tokensIn, uint256 minAmountOut) external {
        if (graduated) revert Graduated();
        (uint256 out,) = quoteSell(tokensIn);
        if (out < minAmountOut) revert Slippage();
        token.transferFrom(msg.sender, address(this), tokensIn);
        boughtBy[msg.sender] -= tokensIn < boughtBy[msg.sender] ? tokensIn : boughtBy[msg.sender];
        tokensSold -= tokensIn;
        (bool ok,) = msg.sender.call{value: out}("");
        require(ok);
    }

    function graduate() external {
        graduated = true;
    }
}

/// Claim recorded for an explicit beneficiary, used to test copying someone else's transaction.
contract ClaimFor is AgentOnly {
    mapping(address => uint256) public claimedOf;

    error NotSelf();

    function claim(address beneficiary, uint256 amount) external payable {
        if (beneficiary != msg.sender) revert NotSelf();
        claimedOf[beneficiary] += amount;
    }
}
