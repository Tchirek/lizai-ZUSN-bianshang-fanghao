# One-time: install this PC's SSH public key on the Debian ADS-B host.
# ssh will prompt for x230's password - type it yourself.
$pub = Get-Content "$env:USERPROFILE\.ssh\id_rsa.pub" -Raw
$pub | ssh -o StrictHostKeyChecking=accept-new x230@192.168.10.167 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo KEY_INSTALLED"
