bucket = "tfstate-rag-portfolio-prod"
key    = "prod/terraform.tfstate"
region = "ap-northeast-1"
use_lockfile = true   # S3 ネイティブ state ロック（並行 apply の破損防止・TF1.10+）
