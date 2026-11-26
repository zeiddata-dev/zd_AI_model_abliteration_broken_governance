function ssh_her { ssh -i "$HOME\.ssh\gpu-box-key.pem" -L 3000:localhost:3000 -L 11434:localhost:11434 ubuntu@YOUR_ELASTIC_IP }
