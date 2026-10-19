// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// 简易测试代币 (ERC20)
contract TestToken {
    string public name = "Arc Test Token";
    string public symbol = "ATT";
    uint8 public decimals = 18;
    uint256 public totalSupply;
    address public owner;
    
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    
    constructor() {
        totalSupply = 1000000 * 10**18;
        balanceOf[msg.sender] = totalSupply;
        owner = msg.sender;
        emit Transfer(address(0), msg.sender, totalSupply);
    }
    
    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);
        return true;
    }
    
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }
    
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        if (allowance[from][msg.sender] != type(uint256).max) {
            allowance[from][msg.sender] -= amount;
        }
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }
}

// 简易 DEX：用原生 USDC 买入 TestToken
// 1 ATT = 0.01 USDC (固定价格，测试用)
contract SimpleDEX {
    TestToken public token;
    uint256 public pricePerToken = 10**16; // 0.01 USDC per token (18 decimals)
    address public owner;
    
    event Bought(address indexed buyer, uint256 usdcPaid, uint256 tokensReceived);
    event Sold(address indexed seller, uint256 tokensSold, uint256 usdcReceived);
    
    constructor(TestToken _token) {
        token = _token;
        owner = msg.sender;
    }
    
    // 用原生 USDC 买入
    function buy() external payable returns (uint256) {
        uint256 usdcPaid = msg.value;
        require(usdcPaid > 0, "Send USDC to buy");
        
        uint256 tokensToGive = (usdcPaid * 10**18) / pricePerToken;
        require(token.balanceOf(address(this)) >= tokensToGive, "Not enough tokens");
        
        token.transfer(msg.sender, tokensToGive);
        emit Bought(msg.sender, usdcPaid, tokensToGive);
        return tokensToGive;
    }
    
    // 卖出代币换回 USDC
    function sell(uint256 tokenAmount) external returns (uint256) {
        require(token.balanceOf(msg.sender) >= tokenAmount, "Not enough tokens");
        
        uint256 usdcToReturn = (tokenAmount * pricePerToken) / 10**18;
        require(address(this).balance >= usdcToReturn, "DEX not enough USDC");
        
        token.transferFrom(msg.sender, address(this), tokenAmount);
        (bool sent, ) = msg.sender.call{value: usdcToReturn}("");
        require(sent, "USDC transfer failed");
        
        emit Sold(msg.sender, tokenAmount, usdcToReturn);
        return usdcToReturn;
    }
    
    // DEX 需要 USDC 流动性来支持卖出
    receive() external payable {}
    
    // owner 可以提取
    function withdraw() external {
        require(msg.sender == owner, "Not owner");
        (bool sent, ) = owner.call{value: address(this).balance}("");
        require(sent, "withdraw failed");
    }
}
