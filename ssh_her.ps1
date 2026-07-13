function ssh_her { ssh -i "$HOME\.ssh\gpu-box-key.pem" -L 11434:localhost:11434 ubuntu@YOUR_ELASTIC_IP }
