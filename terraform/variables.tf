variable "environment" {
  type        = string
  description = "環境名（dev / stag / prod）"
}

variable "aws_region" {
  type        = string
  description = "AWSリージョン"
  default     = "ap-northeast-1"
}

variable "account_id" {
  type        = string
  description = "AWSアカウントID"
}

variable "vector_store_type" {
  type        = string
  description = "ベクトルストアの種類（opensearch or s3_vectors）"
  default     = "s3_vectors"
}

variable "opensearch_scheduled" {
  type        = bool
  description = "OpenSearchの自動起動・停止を有効にするか"
  default     = false
}

variable "enable_private_networking" {
  type        = bool
  description = "プライベートネットワーキングを有効化するか（LambdaのVPC配置＋VPCエンドポイント経由のプライベート通信。層3の経路隔離）"
  default     = false
}
