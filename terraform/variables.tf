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
  default     = "opensearch"
}

variable "opensearch_scheduled" {
  type        = bool
  description = "OpenSearchの自動起動・停止を有効にするか"
  default     = false
}

variable "enable_vpc_endpoints" {
  type        = bool
  description = "インターフェース型VPCエンドポイントを作成するか"
  default     = false
}