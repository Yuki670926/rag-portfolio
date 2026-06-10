bucket = "tfstate-rag-portfolio-stag"
key    = "stag/terraform.tfstate"
region = "ap-northeast-1"
use_lockfile = true   # S3 ネイティブ state ロック（並行 apply の破損防止・TF1.10+）
