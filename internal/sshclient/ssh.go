package sshclient

import (
	"bytes"
	"fmt"
	"os"
	"time"

	"golang.org/x/crypto/ssh"
)

// Client предоставляет методы для выполнения команд по SSH
type Client struct {
	User       string
	Host       string
	Port       int
	SSHKeyPath string
	Timeout    time.Duration
}

// NewClient создает экземпляр SSH-клиента
func NewClient(user, host string, port int, keyPath string, timeout time.Duration) *Client {
	if timeout == 0 {
		timeout = 10 * time.Second // Дефолтный таймаут на подключение
	}
	return &Client{
		User:       user,
		Host:       host,
		Port:       port,
		SSHKeyPath: keyPath,
		Timeout:    timeout,
	}
}

// getAuthMethod считывает приватный ключ с диска
func (c *Client) getAuthMethod() (ssh.AuthMethod, error) {
	key, err := os.ReadFile(c.SSHKeyPath)
	if err != nil {
		return nil, fmt.Errorf("unable to read private key file: %w", err)
	}

	signer, err := ssh.ParsePrivateKey(key)
	if err != nil {
		return nil, fmt.Errorf("unable to parse private key: %w", err)
	}

	return ssh.PublicKeys(signer), nil
}

// RunCommand подключается к ВМ и выполняет указанную команду
func (c *Client) RunCommand(cmd string) (stdout string, stderr string, err error) {
	authMethod, err := c.getAuthMethod()
	if err != nil {
		return "", "", err
	}

	sshConfig := &ssh.ClientConfig{
		User:            c.User,
		Auth:            []ssh.AuthMethod{authMethod},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(), // Для закрытых контуров ВМ
		Timeout:         c.Timeout,
	}

	address := fmt.Sprintf("%s:%d", c.Host, c.Port)
	client, err := ssh.Dial("tcp", address, sshConfig)
	if err != nil {
		return "", "", fmt.Errorf("ssh dial failed to %s: %w", address, err)
	}
	defer client.Close()

	session, err := client.NewSession()
	if err != nil {
		return "", "", fmt.Errorf("failed to create ssh session: %w", err)
	}
	defer session.Close()

	var stdoutBuf, stderrBuf bytes.Buffer
	session.Stdout = &stdoutBuf
	session.Stderr = &stderrBuf

	err = session.Run(cmd)
	return stdoutBuf.String(), stderrBuf.String(), err
}