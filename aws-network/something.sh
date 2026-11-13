pip install "moto[server]" awscli
moto_server -p 5555 &
export AWS_ENDPOINT_URL=http://127.0.0.1:5555 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=ap-south-1
bash infra/aws/01_network.sh && bash infra/aws/verify_network.sh