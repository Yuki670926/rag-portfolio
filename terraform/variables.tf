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
  description = "ベクトルストア構成：s3_vectors（KB のみ）/ opensearch（自前のみ）/ dual（両方＝fast/高精度の二段検索）"
  default     = "s3_vectors"

  validation {
    condition     = contains(["s3_vectors", "opensearch", "dual"], var.vector_store_type)
    error_message = "vector_store_type は s3_vectors / opensearch / dual のいずれかにしてください。"
  }
}

variable "enable_private_networking" {
  type        = bool
  description = "プライベートネットワーキングを有効化するか（LambdaのVPC配置＋VPCエンドポイント経由のプライベート通信。層3の経路隔離）"
  default     = false
}
