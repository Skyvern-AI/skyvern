import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
export declare const FileNode: core.serialization.ObjectSchema<serializers.FileNode.Raw, Skyvern.FileNode>;
export declare namespace FileNode {
    interface Raw {
        type: string;
        size?: number | null;
        mime_type?: string | null;
        content_hash?: string | null;
        created_at: string;
        children?: Record<string, serializers.FileNode.Raw | null | undefined> | null;
    }
}
