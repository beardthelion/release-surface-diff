package lib

const Version = "1.0.0"

var DefaultTimeout = 30

type Client struct{}

func NewClient() *Client { return &Client{} }

func (c *Client) Connect() {}

func (c *Client) hidden() {}
